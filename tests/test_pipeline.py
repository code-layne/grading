"""Read, score and verify on the synthetic class set, against the fake client
(expected-scores.csv's responses, with four injected faults)."""

import json
from pathlib import Path

from grading import checks, evaluate
from grading.fake import DEFAULT_FAULTS, FakeTransport
from grading.pipeline import Pipeline

from conftest import EXPECTED, read_csv

ASSIGNMENT = "alg2-u3-l1-gs"


def run(cfg, conn, transport=None, **kw):
    transport = transport or FakeTransport(EXPECTED)
    summary = Pipeline(cfg, conn, transport, out=lambda *a: None, sleep=lambda s: None, **kw).run()
    return summary, transport


def score(conn, tid, q):
    return conn.execute("SELECT * FROM scores WHERE tracking_id = ? AND question = ?", (tid, q)).fetchone()


def reasons(conn, tid, q):
    row = conn.execute("SELECT reasons FROM review_queue WHERE tracking_id = ? AND question = ?",
                       (tid, q)).fetchone()
    return json.loads(row["reasons"]) if row else []


def test_full_run_catches_every_injected_fault(class_set):
    cfg, conn = class_set
    s, t = run(cfg, conn)
    # 96 crops, one blank (no call); one illegible read gets no verify;
    # two gs8 answers are identical and share one rubric call.
    assert s.mode == "batch" and t.store["submissions"] == 3
    assert s.calls == {"read": 95, "rubric": 11, "verify": 94}
    assert s.waiting == 0

    rep = evaluate.compare(cfg, conn, EXPECTED)
    assert rep["by_type"]["choice"] == {"items": 48, "agree": 48, "disagree": 0, "no_score": 0, "missing": 0,
                                        "review": 1}
    assert rep["by_type"]["short"] == {"items": 36, "agree": 35, "disagree": 0, "no_score": 1, "missing": 0,
                                       "review": 3}
    assert rep["by_type"]["rubric"] == {"items": 12, "agree": 11, "disagree": 1, "no_score": 0, "missing": 0,
                                        "review": 12}
    (d,) = rep["disagreements"]
    assert (d["tracking_id"], d["item"], d["points"], d["expected"]) == ("TYME2T", "gs8", 2.0, 1.0)
    assert d["in_review"] and Path(d["crop"]).is_file()
    assert rep["review"] == {"rubric": 12, "verify-disagreement": 2, "blank": 1, "illegible": 1,
                             "low-confidence": 1}
    assert rep["review_items"] == 16

    # Each fault lands where it should.
    assert reasons(conn, *DEFAULT_FAULTS["illegible"]) == ["illegible"]
    assert score(conn, *DEFAULT_FAULTS["illegible"])["points"] is None
    assert reasons(conn, *DEFAULT_FAULTS["low_confidence"]) == ["low-confidence"]
    assert score(conn, *DEFAULT_FAULTS["low_confidence"])["points"] == 2
    assert reasons(conn, *DEFAULT_FAULTS["verify_disagreement"]) == ["verify-disagreement"]
    assert reasons(conn, *DEFAULT_FAULTS["rubric_off_by_one"]) == ["verify-disagreement", "rubric"]

    for call_type in ("read", "rubric", "verify"):
        spent = rep["spent"][call_type]
        assert spent["usd"] > 0 and spent["cache_read_tokens"] > 0 and spent["unpriced"] == 0


def test_settled_items_are_final_and_rubric_items_all_wait_for_review(class_set):
    cfg, conn = class_set
    run(cfg, conn)
    rows = conn.execute("SELECT s.*, q.type FROM scores s JOIN questions q USING (assignment, question)").fetchall()
    assert len(rows) == 96
    for r in rows:
        if r["type"] == "rubric":
            assert r["status"] == "review"
    finals = [r for r in rows if r["status"] == "final"]
    assert len(finals) == 96 - 16
    for r in finals:
        detail = json.loads(r["detail"])
        assert detail["read"]["confidence"] == "high" and detail["verify"]["agrees"]
        assert detail["verify"]["reading"] is not None


def test_review_queue_puts_disagreements_first(class_set):
    cfg, conn = class_set
    run(cfg, conn)
    queue = conn.execute("SELECT * FROM review_queue ORDER BY priority, assignment, question, normalized_answer"
                         ).fetchall()
    assert [(r["tracking_id"], r["question"]) for r in queue[:2]] == [("2US2Q7", "gs1"), ("TYME2T", "gs8")]
    assert queue[-1]["tracking_id"] == "6VVV2P" and json.loads(queue[-1]["reasons"]) == ["blank"]
    tyme = queue[1]
    assert (tyme["points"], tyme["verify_points"], tyme["max_points"]) == (2, 1, 4)
    detail = json.loads(tyme["detail"])
    assert set(detail["criteria"]) == {"step", "reason", "corrected", "answer"}
    assert detail["criteria"]["step"]["evidence"]
    assert tyme["feedback"].endswith("?") or "?" in tyme["feedback"]
    # Grouping for review: identical answers share a normalized_answer.
    groups = conn.execute("SELECT * FROM review_groups WHERE question = 'gs8' AND n = 2").fetchall()
    assert len(groups) == 1


def test_standard_api_below_the_batch_threshold_gives_the_same_scores(class_set):
    cfg, conn = class_set
    cfg.api.batch_threshold = 1000
    s, t = run(cfg, conn)
    assert s.mode == "standard" and t.store["submissions"] == 0
    assert s.calls == {"read": 95, "rubric": 11, "verify": 94}
    rep = evaluate.compare(cfg, conn, EXPECTED)
    assert [rep["by_type"][k]["agree"] for k in ("choice", "short", "rubric")] == [48, 35, 11]
    modes = {r[0] for r in conn.execute("SELECT DISTINCT mode FROM api_calls")}
    assert modes == {"standard"}


def test_identical_rubric_answers_are_scored_once(class_set):
    cfg, conn = class_set
    s, t = run(cfg, conn)
    rubric_keys = [k for k in t.store["requests"] if k.startswith("rubric-")]
    assert len(rubric_keys) == 11
    a, b = score(conn, "BKTRTN", "gs8"), score(conn, "462HAN", "gs8")
    assert a["normalized_answer"] == b["normalized_answer"]
    assert a["points"] == b["points"] == 3
    assert len([k for k in rubric_keys if "BKTRTN" in k or "462HAN" in k]) == 1


def test_rulings_apply_before_review(class_set):
    cfg, conn = class_set
    tyme = next(r for r in read_csv(EXPECTED) if r["tracking_id"] == "TYME2T" and r["item"] == "gs8")
    with conn:
        conn.execute("INSERT INTO rulings (assignment, question, normalized_answer, points, feedback, decided_at) "
                     "VALUES (?, 'gs7', ?, 1, 'Check the second exponent.', '2026-10-21')",
                     (ASSIGNMENT, checks.normalize("short", "6, 3")))
        conn.execute("INSERT INTO rulings (assignment, question, normalized_answer, points, feedback, decided_at) "
                     "VALUES (?, 'gs8', ?, 1, 'Which step?', '2026-10-21')",
                     (ASSIGNMENT, checks.normalize("rubric", tyme["response"])))
    faults = {k: v for k, v in DEFAULT_FAULTS.items() if k != "rubric_off_by_one"}
    s, t = run(cfg, conn, FakeTransport(EXPECTED, faults))
    sixes = [r for r in read_csv(EXPECTED) if r["item"] == "gs7" and r["response"] == "6, 3"]
    assert len(sixes) == 3
    for r in sixes:
        row = score(conn, r["tracking_id"], "gs7")
        assert (row["points"], row["source"], row["status"]) == (1, "ruling", "final")
        assert row["feedback"] == "Check the second exponent."
        # A ruling settles the item: no verify call, no review.
        assert f"verify-{ASSIGNMENT}-{r['tracking_id']}-gs7" not in t.store["requests"]
    # The rubric ruling replaces the rubric call and the review.
    assert not any("TYME2T" in k for k in t.store["requests"] if k.startswith("rubric-"))
    row = score(conn, "TYME2T", "gs8")
    assert (row["points"], row["source"], row["status"]) == (1, "ruling", "final")
    assert s.calls["rubric"] == 10


def test_x_equals_answer_is_accepted(class_set):
    cfg, conn = class_set
    run(cfg, conn, FakeTransport(EXPECTED, overrides={("TNEJN8", "gs6"): "x = 3/2"}))
    row = score(conn, "TNEJN8", "gs6")
    assert (row["points"], row["status"]) == (1, "final")
    assert json.loads(row["detail"])["read"]["reading"] == "x = 3/2"


def test_unparseable_reading_goes_to_review_never_to_zero(class_set):
    cfg, conn = class_set
    s, t = run(cfg, conn, FakeTransport(EXPECTED, overrides={("44QB73", "gs5"): "see my work below"}))
    row = score(conn, "44QB73", "gs5")
    assert row["points"] is None and row["status"] == "review"
    assert reasons(conn, "44QB73", "gs5") == ["unparseable"]
    assert f"verify-{ASSIGNMENT}-44QB73-gs5" not in t.store["requests"]


def test_refused_call_goes_to_review_and_is_not_retried(class_set):
    cfg, conn = class_set
    store = {}
    fake = FakeTransport(EXPECTED, refuse={("44QB73", "gs1", "read")}, store=store)
    run(cfg, conn, fake)
    assert reasons(conn, "44QB73", "gs1") == ["read-failed"]
    assert conn.execute("SELECT status FROM api_calls WHERE request_key = ?",
                        (f"read-{ASSIGNMENT}-44QB73-gs1",)).fetchone()[0] == "refusal"
    before = len(store["requests"])
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store))
    assert len(store["requests"]) == before and sum(s.calls.values()) == 0


def test_transient_error_waits_for_the_next_run(class_set):
    cfg, conn = class_set
    store = {}
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, fail_once={("44QB73", "gs2", "read")}, store=store))
    assert s.waiting == 1 and score(conn, "44QB73", "gs2") is None
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store))
    assert s.calls == {"read": 1, "rubric": 0, "verify": 1} and s.mode == "standard"
    assert score(conn, "44QB73", "gs2")["status"] == "final"


def test_interrupted_batch_is_collected_not_resubmitted(class_set):
    cfg, conn = class_set
    store = {}
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store), wait=False)
    assert s.batch_pending == "msgbatch_fake001" and store["submissions"] == 1
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "in_progress"
    assert conn.execute("SELECT count(*) FROM readings").fetchone()[0] == 0
    assert s.waiting == 95

    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store))
    assert store["submissions"] == 3
    assert [r[0] for r in conn.execute("SELECT status FROM batches ORDER BY id")] == ["collected"] * 3
    reads = conn.execute("SELECT count(*), count(DISTINCT request_key) FROM api_calls WHERE call_type = 'read'"
                         ).fetchone()
    assert tuple(reads) == (95, 95)
    assert s.waiting == 0


def test_rerun_sends_nothing_and_keeps_review_decisions(class_set):
    cfg, conn = class_set
    store = {}
    run(cfg, conn, FakeTransport(EXPECTED, store=store))
    with conn:
        conn.execute("UPDATE scores SET points = 3, source = 'review', status = 'final' "
                     "WHERE tracking_id = 'TYME2T' AND question = 'gs8'")
        conn.execute("UPDATE review_queue SET resolved_at = '2026-10-23' WHERE tracking_id = 'TYME2T' "
                     "AND question = 'gs8'")
    before = len(store["requests"])
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store))
    assert len(store["requests"]) == before and sum(s.calls.values()) == 0
    row = score(conn, "TYME2T", "gs8")
    assert (row["points"], row["source"]) == (3, "review")
    assert conn.execute("SELECT resolved_at FROM review_queue WHERE tracking_id = 'TYME2T' AND question = 'gs8'"
                        ).fetchone()[0] == "2026-10-23"


def test_confirmed_blank_is_final_zero(class_set):
    cfg, conn = class_set
    with conn:
        conn.execute("UPDATE crops SET blank_confirmed = 1 WHERE blank = 1")
    run(cfg, conn)
    row = score(conn, "6VVV2P", "gs5")
    assert (row["points"], row["status"]) == (0, "final")


def test_light_pencil_blank_is_read(class_set):
    cfg, conn = class_set
    with conn:
        conn.execute("UPDATE crops SET blank_confirmed = 0 WHERE blank = 1")
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, overrides={("6VVV2P", "gs5"): "3x^3 + 4x^2 - 5x - 4"}))
    assert s.calls["read"] == 96
    assert score(conn, "6VVV2P", "gs5")["points"] == 2


# ---- waiting on a batch, --now and grading batch cancel ---------------------

def test_elapsed_reads_as_hours_and_minutes():
    from grading.pipeline import elapsed
    assert elapsed(0) == "0m" and elapsed(59) == "0m" and elapsed(12 * 60 + 30) == "12m"
    assert elapsed(2 * 3600 + 4 * 60 + 10) == "2h 04m" and elapsed(-5) == "0m"


class Clock:
    """A clock that moves only when the pipeline sleeps."""

    def __init__(self):
        import datetime as dt
        self.now = dt.datetime.now().replace(microsecond=0)
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        import datetime as dt
        self.slept.append(seconds)
        self.now += dt.timedelta(seconds=seconds)


def test_poll_lines_show_elapsed_time_at_least_every_five_minutes(class_set):
    cfg, conn = class_set
    cfg.api.batch_poll_seconds = 30
    clock, lines = Clock(), []
    # 25 polls of 30 s with no change in the count: a quiet 12 minutes.
    Pipeline(cfg, conn, FakeTransport(EXPECTED, polls_before_end=25), out=lines.append, sleep=clock.sleep,
             clock=clock, on_wait=["Ctrl-C is safe: `grading read` picks it up."]).run()
    first = [line for line in lines if "msgbatch_fake001" in line and "done" in line]
    assert first == ["batch msgbatch_fake001: in_progress, 0 of 95 done, 0m",
                     "batch msgbatch_fake001: in_progress, 0 of 95 done, 5m",
                     "batch msgbatch_fake001: in_progress, 0 of 95 done, 10m"]
    # The resume hint comes with the first poll line, once per run.
    i = lines.index(first[0])
    assert lines[i + 1] == "  Ctrl-C is safe: `grading read` picks it up."
    assert sum("Ctrl-C" in line for line in lines) == 1


def test_a_long_poll_interval_still_prints_every_five_minutes(class_set):
    cfg, conn = class_set
    cfg.api.batch_poll_seconds = 3600
    clock, lines = Clock(), []
    Pipeline(cfg, conn, FakeTransport(EXPECTED, polls_before_end=3), out=lines.append, sleep=clock.sleep,
             clock=clock).run()
    assert set(clock.slept) == {300}
    assert [line.rsplit(", ", 1)[1] for line in lines if "msgbatch_fake001" in line and "done" in line] == [
        "0m", "5m", "10m"]


def test_elapsed_time_counts_from_submission_across_runs(class_set):
    import datetime as dt
    cfg, conn = class_set
    store = {}
    run(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=100), wait=False)
    conn.execute("UPDATE batches SET submitted_at = ?",
                 ((dt.datetime.now() - dt.timedelta(hours=2, minutes=4, seconds=20)).isoformat(timespec="seconds"),))
    conn.commit()
    lines = []
    Pipeline(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=100), out=lines.append,
             wait=False).run()
    assert "batch msgbatch_fake001: in_progress, 0 of 95 done, 2h 04m" in lines


def test_now_never_waits_on_a_batch_still_running(class_set):
    import pytest
    from grading.pipeline import BatchPending
    cfg, conn = class_set
    store = {}
    run(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=100), wait=False)
    with pytest.raises(BatchPending) as e:
        run(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=100), force_mode="standard")
    assert e.value.batch_id == "msgbatch_fake001" and e.value.status == "in_progress"
    # Nothing was sent twice, and the batch is still there to cancel or collect.
    assert store["submissions"] == 1 and store["requests"] == []
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "in_progress"


def test_now_collects_a_batch_that_has_ended_then_carries_on(class_set):
    cfg, conn = class_set
    store = {}
    run(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=1), wait=False)
    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store), force_mode="standard")
    assert store["submissions"] == 1 and s.waiting == 0
    assert conn.execute("SELECT count(*) FROM api_calls WHERE mode = 'batch'").fetchone()[0] == 95
    assert conn.execute("SELECT count(*) FROM api_calls WHERE mode = 'standard' AND call_type = 'read'"
                        ).fetchone()[0] == 0


def test_cancel_keeps_what_ran_and_now_sends_only_the_rest(class_set):
    cfg, conn = class_set
    store = {}
    run(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=100), wait=False)
    lines = []
    done = Pipeline(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=100, ran_before_cancel=40),
                    out=lines.append, sleep=lambda s: None).cancel_batches()
    assert done == [{"id": "msgbatch_fake001", "status": "canceled", "results": 40, "canceled": 55}]
    assert store["canceled"] == ["msgbatch_fake001"] and "canceling batch msgbatch_fake001" in lines
    row = conn.execute("SELECT * FROM batches").fetchone()
    assert row["status"] == "canceled" and row["canceled_at"] and row["collected_at"]
    assert conn.execute("SELECT count(*) FROM readings WHERE pass = 'read'").fetchone()[0] == 40
    never_ran = {r[0] for r in conn.execute("SELECT request_key FROM api_calls WHERE status = 'canceled'")}
    assert len(never_ran) == 55

    s, _ = run(cfg, conn, FakeTransport(EXPECTED, store=store), force_mode="standard")
    assert store["submissions"] == 1 and s.mode == "standard"
    sent = {r[0] for r in conn.execute("SELECT request_key FROM api_calls WHERE mode = 'standard' "
                                       "AND call_type = 'read'")}
    assert sent == never_ran
    reads = conn.execute("SELECT count(*), count(DISTINCT request_key) FROM api_calls "
                         "WHERE call_type = 'read' AND status = 'ok'").fetchone()
    assert tuple(reads) == (95, 95)
    # The same outcome as a run that was never canceled.
    assert s.waiting == 0 and s.final == 80 and s.in_review == 16


def test_cancel_of_a_batch_that_already_ended_just_collects_it(class_set):
    cfg, conn = class_set
    store = {}
    run(cfg, conn, FakeTransport(EXPECTED, store=store, polls_before_end=1), wait=False)
    done = Pipeline(cfg, conn, FakeTransport(EXPECTED, store=store), out=lambda *a: None,
                    sleep=lambda s: None).cancel_batches()
    assert done == [{"id": "msgbatch_fake001", "status": "collected", "results": 95, "canceled": 0}]
    assert "canceled" not in store


def test_an_interrupted_cancel_is_finished_by_the_next_run(class_set):
    import pytest
    from grading.pipeline import BatchPending
    cfg, conn = class_set
    store = {}
    fake = FakeTransport(EXPECTED, store=store, polls_before_end=100, ran_before_cancel=10)
    run(cfg, conn, fake, wait=False)
    # Canceled, then interrupted before the batch ended.
    fake.batch_cancel("msgbatch_fake001")
    conn.execute("UPDATE batches SET status = 'canceling'")
    conn.commit()
    with pytest.raises(BatchPending) as e:  # still canceling: --now won't wait on it
        run(cfg, conn, fake, force_mode="standard")
    assert e.value.status == "canceling"
    s, _ = run(cfg, conn, fake, force_mode="standard")
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "canceled"
    assert conn.execute("SELECT count(*) FROM api_calls WHERE mode = 'standard' AND call_type = 'read'"
                        ).fetchone()[0] == 85
    assert s.waiting == 0


def test_a_run_waiting_on_a_batch_canceled_elsewhere_stops_without_resubmitting(class_set):
    cfg, conn = class_set
    store = {}

    class CanceledElsewhere(FakeTransport):
        def batch_status(self, batch_id):
            if self.store["batches"][batch_id]["polls"] == 2 and "ran" not in self.store["batches"][batch_id]:
                self.batch_cancel(batch_id)   # `grading batch cancel` in another terminal
                conn.execute("UPDATE batches SET status = 'canceling'")
                conn.commit()
            return super().batch_status(batch_id)

    lines = []
    s = Pipeline(cfg, conn, CanceledElsewhere(EXPECTED, store=store, polls_before_end=100, ran_before_cancel=30),
                 out=lines.append, sleep=lambda s: None).run()
    assert store["submissions"] == 1 and "batch msgbatch_fake001 was canceled; stopping here" in lines
    assert conn.execute("SELECT status FROM batches").fetchone()[0] == "canceled"
    assert s.calls["read"] == 95 and s.waiting > 0
