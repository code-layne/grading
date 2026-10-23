"""Standard API calls sent several at once (--now): the concurrency limit,
rate limits, progress lines, Ctrl-C, and results identical to sending one
call at a time. Against the fake client, on the synthetic class set."""

import datetime as dt
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import threading
import time

import pytest

from grading import costs, db, evaluate
from grading.apiclient import RecordingTransport, ReplayTransport
from grading.fake import FakeTransport
from grading.pipeline import Pipeline

from conftest import EXPECTED, fake_run, synthetic_cfg

CALLS = {"read": 95, "rubric": 11, "verify": 94}


def fresh(ingested, root):
    """Another copy of the ingested class set."""
    shutil.copytree(ingested / "data", root / "data")
    cfg = synthetic_cfg(root)
    return cfg, db.connect(cfg.database)


def run(cfg, conn, transport, **kw):
    lines = []
    kw.setdefault("sleep", lambda s: None)
    summary = Pipeline(cfg, conn, transport, out=lines.append, force_mode="standard", **kw).run()
    return summary, lines


def report(cfg, conn):
    """The eval report, without the paths that differ between workdirs."""
    rep = evaluate.compare(cfg, conn, EXPECTED)
    for d in rep["disagreements"]:
        d.pop("crop", None)
    rep.pop("data")
    return rep


def snapshot(conn):
    """Every row read, score and verify wrote, without ids and timestamps,
    in no particular order; and the tokens by call type."""
    out = {}
    for table in ("readings", "judgments", "scores", "review_queue"):
        rows = [{k: r[k] for k in r.keys() if k not in ("id", "created_at", "scored_at")}
                for r in conn.execute(f"SELECT * FROM {table}")]
        out[table] = sorted(json.dumps(r, sort_keys=True) for r in rows)
    out["api_calls"] = sorted(tuple(r) for r in conn.execute(
        "SELECT request_key, call_type, crop_id, mode, batch_id, model, status, error FROM api_calls"))
    out["tokens"] = [tuple(r) for r in conn.execute(
        "SELECT call_type, sum(input_tokens), sum(output_tokens), sum(cache_write_5m_tokens), "
        "sum(cache_read_tokens) FROM api_calls GROUP BY call_type ORDER BY call_type")]
    return out


def recording(directory):
    """Each recorded call, without its usage (which call of a question pays
    for the cache write depends on which one goes first)."""
    out = {}
    for f in sorted((directory / "calls").glob("*.json")):
        entry = json.loads(f.read_text())
        if entry.get("response"):
            entry["response"].pop("usage")
        out[f.name] = entry
    return out


@pytest.mark.parametrize("limit", [1, 3, 8])
def test_concurrency_limit_is_honored(class_set, limit):
    cfg, conn = class_set
    cfg.api.concurrency = limit
    fake = FakeTransport(EXPECTED, delay=0.004)
    s, lines = run(cfg, conn, fake)
    assert fake.store["max_in_flight"] == limit
    assert s.calls == CALLS and s.waiting == 0 and s.mode == "standard"
    assert lines[0] == f"sending through the standard API, up to {limit} calls at a time"


def test_concurrent_run_is_identical_to_one_call_at_a_time(ingested, tmp_path):
    results = {}
    for limit in (1, 8):
        cfg, conn = fresh(ingested, tmp_path / str(limit))
        cfg.api.concurrency = limit
        rec = tmp_path / f"rec{limit}"
        fake = FakeTransport(EXPECTED, delay=0.002)
        s, _ = run(cfg, conn, RecordingTransport(fake, rec))
        assert s.calls == CALLS and fake.store["max_in_flight"] == limit
        assert json.loads((rec / "recording.json").read_text())["complete"] is True
        results[limit] = (snapshot(conn), recording(rec), report(cfg, conn), fake.store["requests"])
        conn.close()
    one, eight = results[1], results[8]
    assert eight[0] == one[0]                      # the same database rows
    assert eight[1] == one[1] and len(one[1]) == 200  # the same recording files
    assert eight[2] == one[2]                      # the same eval report, dollars included
    assert [k for k in eight[3] if k.startswith("rubric-")] != [] and sorted(eight[3]) == sorted(one[3])


class Watching(FakeTransport):
    """Checks, as each call goes out, that the crop's read is already stored
    (through a connection of its own), and notes the call types in flight."""

    def __init__(self, *a, database, **kw):
        super().__init__(*a, **kw)
        self.database = database
        self.flying, self.overlaps, self.early = [], set(), []
        self.watch = threading.Lock()

    def create(self, key, params, meta):
        if meta["call_type"] != "read":
            c = sqlite3.connect(self.database)
            stored = c.execute("SELECT count(*) FROM readings WHERE crop_id = ? AND pass = 'read'",
                               (meta["crop_id"],)).fetchone()[0]
            c.close()
            if not stored:
                self.early.append(key)
        with self.watch:
            self.overlaps.update((t, meta["call_type"]) for t in self.flying)
            self.flying.append(meta["call_type"])
        try:
            time.sleep(0.05 if meta["call_type"] == "read" else 0.002)
            return super().create(key, params, meta)
        finally:
            with self.watch:
                self.flying.remove(meta["call_type"])


def test_a_crops_verify_and_rubric_calls_wait_for_its_read_while_other_crops_go_on(class_set):
    cfg, conn = class_set
    fake = Watching(EXPECTED, database=cfg.database)
    s, _ = run(cfg, conn, fake)
    assert s.calls == CALLS
    assert fake.early == []
    # Later crops were still being read while earlier ones went on to their
    # rubric or verify calls.
    assert {("read", "rubric"), ("read", "verify")} & fake.overlaps


def test_a_429_storm_recovers_with_fewer_calls_in_flight(ingested, tmp_path):
    cfg, conn = fresh(ingested, tmp_path / "calm")
    run(cfg, conn, FakeTransport(EXPECTED))
    calm = report(cfg, conn)

    cfg, conn = fresh(ingested, tmp_path / "storm")
    fake = FakeTransport(EXPECTED, delay=0.002, rate_limit=24, retry_after=0.2)
    start = time.monotonic()
    s, lines = run(cfg, conn, fake)
    took = time.monotonic() - start
    assert fake.store["rate_limited"] == 24
    assert s.calls == CALLS and s.waiting == 0
    # The first eight 429s at once cut the limit once, then each round of
    # retries cuts it again, down to one call at a time.
    notes = [line for line in lines if line.startswith("rate limited")]
    assert notes == [f"rate limited (429): waiting 0.2s, then {n} calls at a time for the rest of the run"
                     for n in (4, 2, 1)]
    # Every call that hit a 429 was sent again and came back; nothing waits.
    assert {r[0] for r in conn.execute("SELECT DISTINCT status FROM api_calls")} == {"ok"}
    assert report(cfg, conn) == calm
    # The Retry-After was honored: once at one call at a time, 24 - 8 - 4 - 2
    # rate limits each held every call back for 0.2s.
    assert took >= 10 * 0.2
    assert fake.store["max_in_flight_after_storm"] == 1


def test_a_call_still_rate_limited_after_its_retries_waits_for_the_next_run(class_set):
    cfg, conn = class_set
    cfg.api.concurrency = 2
    store = {}
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store, rate_limit=10_000, retry_after=0))
    assert s.calls == {"read": 95, "rubric": 0, "verify": 0} and s.waiting == 95
    assert {r[0] for r in conn.execute("SELECT DISTINCT status FROM api_calls")} == {"rate-limited"}
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=dict(store, rate_limited=0)))
    assert s.calls == CALLS and s.waiting == 0


def test_the_order_of_database_rows_does_not_change_the_eval_report(graded):
    cfg, conn = graded
    before = evaluate.compare(cfg, conn, EXPECTED)
    shuffle = random.Random(7)
    with conn:
        for table in ("api_calls", "readings", "judgments", "scores", "review_queue"):
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
            shuffle.shuffle(rows)
            conn.execute(f"DELETE FROM {table}")
            for r in rows:
                r.pop("id", None)  # new ids, in the shuffled order
                conn.execute(f"INSERT INTO {table} ({', '.join(r)}) VALUES ({', '.join('?' * len(r))})",
                             list(r.values()))
    assert [r[0] for r in conn.execute("SELECT request_key FROM api_calls ORDER BY id")] != \
        sorted(r[0] for r in conn.execute("SELECT request_key FROM api_calls"))
    after = evaluate.compare(cfg, conn, EXPECTED)
    assert after == before
    assert evaluate.lines(after) == evaluate.lines(before)


def test_a_progress_line_per_pass_at_least_every_15_seconds_and_a_final_one(class_set):
    cfg, conn = class_set
    fake = FakeTransport(EXPECTED)
    base = dt.datetime(2026, 10, 23, 9, 0)
    # A second passes with every call made.
    s, lines = run(cfg, conn, fake, clock=lambda: base + dt.timedelta(seconds=len(fake.store["requests"])))
    shape = re.compile(r"^(read|rubric|verify): (\d+) of (\d+) done, (\d+) failed, ((\d+)m )?(\d+)s, "
                       r"\$(\d+\.\d\d)( so far|; finished)$")
    progress = [m for m in map(shape.match, lines) if m]
    first = next(m for m in progress if m[1] == "read")
    assert first[0].startswith("read: ") and int(first[3]) == 95 and first[9] == " so far"
    # At least every 15 seconds while a pass is under way.
    for p in ("read", "rubric", "verify"):
        times = [int(m[6] or 0) * 60 + int(m[7]) for m in progress if m[1] == p]
        assert times and all(b - a <= 15 for a, b in zip(times, times[1:]))
    # The totals settle on the calls made, and one final line per pass gives the cost.
    spent = costs.spent(conn, cfg.prices)
    for p, n in CALLS.items():
        (final,) = [m for m in progress if m[1] == p and m[9] == "; finished"]
        assert (int(final[2]), int(final[3]), int(final[4])) == (n, n, 0)
        assert final[8] == f"{spent[p]['usd']:.2f}"
        totals = [int(m[3]) for m in progress if m[1] == p]
        assert totals == sorted(totals, reverse=True)


def test_progress_lines_keep_coming_while_no_call_returns(class_set):
    cfg, conn = class_set
    cfg.api.concurrency = 1

    class Slow(FakeTransport):
        def create(self, key, params, meta):
            if not self.store["requests"]:
                time.sleep(2.2)
            return super().create(key, params, meta)

    start = time.monotonic()
    base = dt.datetime(2026, 10, 23, 9, 0)
    # The clock runs 30 times fast: 15 seconds go by every half second.
    s, lines = run(cfg, conn, Slow(EXPECTED), clock=lambda: base + dt.timedelta(
        seconds=30 * (time.monotonic() - start)))
    assert sum(line.startswith("read: 0 of 95 done, 0 failed") for line in lines) >= 2


class CtrlC(FakeTransport):
    """Presses Ctrl-C (a real SIGINT) once `after` calls have come back."""

    def __init__(self, *a, after, **kw):
        super().__init__(*a, **kw)
        self.after, self.pressed = after, threading.Event()

    def create(self, key, params, meta):
        out = super().create(key, params, meta)
        if len(self.store["requests"]) >= self.after and not self.pressed.is_set():
            self.pressed.set()
            os.kill(os.getpid(), signal.SIGINT)
        return out


def test_ctrl_c_keeps_what_came_back_and_the_rerun_sends_only_the_rest(ingested, tmp_path):
    cfg, conn = fresh(ingested, tmp_path / "whole")
    run(cfg, conn, FakeTransport(EXPECTED))
    whole = report(cfg, conn)

    cfg, conn = fresh(ingested, tmp_path / "cut")
    store = {}
    rec = tmp_path / "rec"
    lines = []
    with pytest.raises(KeyboardInterrupt):
        Pipeline(cfg, conn, RecordingTransport(CtrlC(EXPECTED, store=store, after=40, delay=0.005), rec),
                 out=lines.append, force_mode="standard", on_stop=["Ctrl-C stops cleanly"]).run()
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
    made = list(store["requests"])
    assert 40 <= len(made) < 200
    # Every call that came back is stored, and nothing else was sent.
    assert sorted(r[0] for r in conn.execute("SELECT request_key FROM api_calls")) == sorted(made)
    assert lines[1] == "  Ctrl-C stops cleanly"
    assert any(line.startswith("stopping: waiting for the") for line in lines)
    assert any(line.startswith("read: ") and line.endswith("; stopped") for line in lines)
    cut = snapshot(conn)

    # The recording holds exactly those calls, each with its response, and
    # says it is incomplete.
    assert json.loads((rec / "recording.json").read_text())["complete"] is False
    files = {f.stem: json.loads(f.read_text()) for f in (rec / "calls").glob("*.json")}
    assert sorted(files) == sorted(made) and all(e["response"] for e in files.values())
    assert not list((rec / "calls").glob("*.tmp"))
    # It replays: what it recorded comes back, the rest is left waiting.
    rcfg, rconn = fresh(ingested, tmp_path / "replay-cut")
    replay = ReplayTransport(rec)
    s, _ = run(rcfg, rconn, replay)
    assert s.waiting > 0 and replay.missing and replay.stale == []
    again = snapshot(rconn)
    for table in ("readings", "judgments"):
        assert again[table] == cut[table]

    # The same command again sends only what is missing.
    s, _ = run(cfg, conn, RecordingTransport(FakeTransport(EXPECTED, store=store), rec))
    assert len(store["requests"]) == len(set(store["requests"])) == 200
    assert s.waiting == 0 and report(cfg, conn) == whole
    # And the recording is now whole: it replays to the same report.
    assert json.loads((rec / "recording.json").read_text())["complete"] is True
    rcfg, rconn = fresh(ingested, tmp_path / "replay-whole")
    replay = ReplayTransport(rec)
    run(rcfg, rconn, replay)
    assert replay.missing == [] and report(rcfg, rconn) == whole


def test_ctrl_c_in_an_eval_and_the_same_command_with_its_workdir_resumes(tmp_path, capsys, monkeypatch):
    from grading import cli
    from test_cli import _synthetic_config
    store = {}
    made = {}

    def transport(api):
        if not made:
            made["t"] = CtrlC(EXPECTED, store=store, after=25, delay=0.005)
        else:
            made["t"] = FakeTransport(EXPECTED, store=store)
        return made["t"]
    monkeypatch.setattr(cli, "AnthropicTransport", transport)
    cfg = _synthetic_config(tmp_path)
    work = str(tmp_path / "eval")
    argv = ["--config", cfg, "eval", "--now", "--yes", "--workdir", work]
    assert cli.main(argv) == 130
    out = capsys.readouterr()
    assert "interrupted" in out.err
    assert (f"Ctrl-C stops cleanly: what came back is kept, and `grading --config {cfg} eval --workdir {work} "
            f"--now --yes` sends the rest.") in out.out
    first = len(store["requests"])
    assert 25 <= first < 200
    assert cli.main(argv) == 0
    assert "Review: 16 items" in capsys.readouterr().out
    assert len(store["requests"]) == len(set(store["requests"])) == 200
