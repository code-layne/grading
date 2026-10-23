"""Read, score and verify: steps 5 to 7 of the design's pipeline.

Every crop on a placed page goes through:

  read    one call per non-blank crop: a transcription, never solved or corrected
  score   choice and short items in code (checks.py); rubric items by a call,
          once per group of identical normalized readings. An existing ruling
          for the normalized answer scores the item before anything else.
  verify  an independent second call per crop, with the crop, the manifest
          entry and the first reading and score, but not the first call's
          reasoning

and ends with a proposed score and, unless it is plainly settled, a row in
review_queue. A choice or short item is settled only when both readings
match with high confidence. Every rubric item goes to review in this phase.

Review's decisions are inputs here like any other: a ruling for the item's
normalized answer settles it (no verify call, no review), a reading fixed by
hand (corrections) replaces the read pass's reading and needs no verify
call, and a confirmed blank scores zero. A score review set for one crop
alone (source 'review') is never recomputed. So the scores and the queue are
always what these inputs give, and review settles items by calling
settle() rather than writing scores itself.

A run over more than api.batch_threshold crops goes through the Batch API
(--now sends any run through the standard API, --batch any run through the
Batch API). There the work runs in rounds, one batch each: every call whose
inputs exist (round 1 the reads, round 2 the rubric calls and the choice and
short verifies, round 3 the rubric verifies). The batch id is stored, so a
rerun after an interruption collects that batch instead of submitting it
again. `grading batch cancel` stops a batch still running and stores the
results it has; the requests that never ran come back canceled and are sent
again by the next run.

Through the standard API, up to api.concurrency calls are in flight at once
(standard.py), and a crop's rubric and verify calls go out as soon as its
read is stored, while other crops are still being read. Each result is
stored as it comes back, on this thread alone. The calls made, and so the
rows stored and the recording, are the same whatever the order: a shared
rubric call is made on the group's first crop, and only once every earlier
crop of that question has been read, exactly as the rounds would make it.

Everything is stored as it arrives, so a rerun only sends what is still
missing.
"""

import datetime as dt
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field

from . import checks, costs, prompts, rubrics, standard
from .apiclient import Outcome
from .manifests import ManifestError

TERMINAL = {"refusal", "truncated", "no-tool-call", "invalid"}  # retrying won't help
PASS_ORDER = {"read": 0, "rubric": 1, "verify": 2}
PRIORITY = {"verify-disagreement": 0, "illegible": 1, "unparseable": 1, "not-a-letter": 1, "not-a-number": 1,
            "read-failed": 1, "rubric-failed": 1, "verify-failed": 1, "reading-corrected": 1,
            "low-confidence": 2, "rubric": 3, "blank": 4}


class CostRefused(Exception):
    pass


class Cancelled(Exception):
    pass


class BatchPending(Exception):
    """A --now run found a batch that has not ended: sending its requests
    through the standard API too would pay for them twice."""

    def __init__(self, batch_id, status):
        self.batch_id, self.status = batch_id, status
        super().__init__(f"batch {batch_id} is {status.replace('_', ' ')}")


HEARTBEAT_SECONDS = 300   # a poll line at least this often, even when nothing changed


def elapsed(seconds: float) -> str:
    """2h 04m, or 12m under an hour."""
    h, m = divmod(int(max(seconds, 0)) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


@dataclass
class Item:
    crop_id: int
    tracking_id: str
    assignment: str
    question: str
    path: str
    blank: bool
    blank_confirmed: int | None
    q: dict
    manifest_path: str

    @property
    def qtype(self):
        return self.q["type"]

    @property
    def needs_read(self):
        return not self.blank or self.blank_confirmed == 0


@dataclass
class Summary:
    mode: str = "standard"
    calls: dict = field(default_factory=lambda: {t: 0 for t in costs.CALL_TYPES})
    final: int = 0
    review: dict = field(default_factory=dict)      # reason -> count
    in_review: int = 0
    waiting: int = 0                                # items with a call still to come
    batch_pending: str | None = None                # submitted, not yet ended (--no-wait)
    estimate: costs.Estimate | None = None


def request_key(call_type: str, item: Item) -> str:
    """The custom_id: ^[A-Za-z0-9_-]{1,64}$, stable across runs and machines."""
    raw = f"{call_type}-{item.assignment}-{item.tracking_id}-{item.question}"
    key = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(key) > 64:
        key = key[:47] + "-" + hashlib.sha1(raw.encode()).hexdigest()[:16]
    return key


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


class Pipeline:
    def __init__(self, cfg, conn, transport, *, confirm=None, wait=True, out=print, sleep=time.sleep,
                 clock=dt.datetime.now, force_mode: str | None = None, on_wait=(), on_stop=()):
        """force_mode: "standard" (--now) or "batch" (--batch); None follows
        api.batch_threshold. on_wait: lines printed under the first poll
        line of a batch this run waits for (how to resume it). on_stop: the
        same for a run through the standard API (what Ctrl-C does)."""
        self.cfg, self.conn, self.transport = cfg, conn, transport
        self.confirm = confirm
        self.wait = wait
        self.out = out
        self.sleep = sleep
        self.clock = clock
        self.force_mode = force_mode
        self.on_wait = list(on_wait)
        self.on_stop = list(on_stop)
        self._hinted = False
        self._rubrics = {}
        self._checked = {}        # (assignment, question, reading) -> checks.Check
        self._groups_sent = set() # rubric groups whose call went out in this run (standard API)
        self._attempted = set()   # keys that failed in this run: retried on the next run, not this one

    # ---- state -----------------------------------------------------------

    def items(self, assignment=None) -> list[Item]:
        sql = ("SELECT c.id, c.tracking_id, c.assignment, c.question, c.path, c.blank, c.blank_confirmed, "
               "q.type, q.points, q.answer, q.check_kind, q.tolerance, q.rubric, a.manifest_path "
               "FROM crops c JOIN pages p ON p.id = c.page_id "
               "JOIN questions q ON q.assignment = c.assignment AND q.question = c.question "
               "JOIN assignments a ON a.assignment = c.assignment WHERE p.status = 'placed'")
        args = ()
        if assignment:
            sql += " AND c.assignment = ?"
            args = (assignment,)
        out = []
        for r in self.conn.execute(sql + " ORDER BY c.id", args):
            q = {"id": r["question"], "type": r["type"], "points": r["points"], "answer": r["answer"],
                 "check": r["check_kind"], "tolerance": r["tolerance"], "rubric": r["rubric"]}
            out.append(Item(r["id"], r["tracking_id"], r["assignment"], r["question"], r["path"],
                            bool(r["blank"]), r["blank_confirmed"], q, r["manifest_path"]))
        return out

    def rubric(self, item: Item):
        k = (item.manifest_path, item.q["rubric"])
        if k not in self._rubrics:
            self._rubrics[k] = rubrics.load(item.manifest_path, item.q["rubric"])
        return self._rubrics[k]

    def _state(self):
        c = self.conn
        s = {"readings": {(r["crop_id"], r["pass"]): r for r in c.execute("SELECT * FROM readings")},
             "judgments": {(r["crop_id"], r["pass"]): r for r in c.execute("SELECT * FROM judgments")},
             "rulings": {(r["assignment"], r["question"], r["normalized_answer"]): r
                         for r in c.execute("SELECT * FROM rulings")},
             "failed": {}}
        s["groups"] = {(r["assignment"], r["question"], r["normalized_answer"]): r
                       for r in s["judgments"].values() if r["pass"] == "rubric"}
        # A reading fixed in review stands in for the read pass's own.
        s["corrected"] = set()
        for r in c.execute("SELECT * FROM corrections"):
            model = s["readings"].get((r["crop_id"], "read"))
            s["readings"][(r["crop_id"], "read")] = {
                "crop_id": r["crop_id"], "pass": "read", "reading": r["reading"], "latex": None,
                "confidence": "high", "illegible": 0, "model": "review", "normalized": r["normalized"],
                "original": model["reading"] if model is not None else None}
            s["corrected"].add(r["crop_id"])
        for r in c.execute("SELECT request_key, status FROM api_calls ORDER BY id"):
            s["failed"][r["request_key"]] = r["status"] if r["status"] != "ok" else None
        # The last rubric call for each group of identical answers.
        s["group_status"] = {}
        for r in c.execute("SELECT a.status, cr.assignment, cr.question, rd.normalized FROM api_calls a "
                           "JOIN crops cr ON cr.id = a.crop_id "
                           "JOIN readings rd ON rd.crop_id = a.crop_id AND rd.pass = 'read' "
                           "WHERE a.call_type = 'rubric' ORDER BY a.id"):
            s["group_status"][(r["assignment"], r["question"], r["normalized"])] = r["status"]
        return s

    def _no_verify(self, it, s, first) -> bool:
        """A reading fixed by hand or a score from a ruling needs no second opinion."""
        return it.crop_id in s["corrected"] or first.get("source") == "ruling"

    def _blocked(self, key, s):
        """True when this call should not be sent now."""
        return s["failed"].get(key) in TERMINAL or key in self._attempted

    def first_score(self, item: Item, s) -> dict:
        """The proposed score from the read pass: points (None when there is
        none), source, criteria and feedback, and why there is no score."""
        r = s["readings"].get((item.crop_id, "read"))
        if r is None:
            return {"points": None, "reason": None}
        if r["illegible"]:
            return {"points": None, "reason": "illegible"}
        ruling = s["rulings"].get((item.assignment, item.question, r["normalized"]))
        if ruling is not None:
            return {"points": ruling["points"], "source": "ruling", "feedback": ruling["feedback"]}
        if item.qtype in ("choice", "short"):
            k = (item.assignment, item.question, r["reading"])
            if k not in self._checked:  # SymPy is slow, and this runs after every standard call
                try:
                    self._checked[k] = checks.score(item.q, r["reading"])
                except ValueError as e:  # the key itself is the problem
                    raise ManifestError(f"{item.assignment} {item.question}: {e}") from None
            chk = self._checked[k]
            return {"points": chk.points, "source": "code", "reason": chk.reason}
        g = (item.assignment, item.question, r["normalized"])
        j = s["groups"].get(g)
        if j is None:
            failed = s["group_status"].get(g) in TERMINAL
            return {"points": None, "reason": "rubric-failed" if failed else None}
        crit = json.loads(j["criteria"])
        return {"points": j["points"], "source": "rubric", "feedback": j["feedback"], "criteria_detail": crit,
                "criteria": {k: v["points"] for k, v in crit.items()}}

    # ---- planning --------------------------------------------------------

    def pending(self, items, s) -> list[tuple[str, Item, str]]:
        """(call type, item, request key) for every call that can be sent now."""
        out = []
        groups = set()
        unread = set()   # questions with a crop whose read is still to come
        for it in items:
            r = s["readings"].get((it.crop_id, "read"))
            if r is None:
                key = request_key("read", it)
                if it.needs_read and not self._blocked(key, s):
                    out.append(("read", it, key))
                    unread.add((it.assignment, it.question))
                continue
            if r["illegible"]:
                continue
            if it.qtype == "rubric":
                # One call per group of identical answers, made on its first
                # crop: which one that is is known only once every earlier
                # crop of the question has been read.
                g = (it.assignment, it.question, r["normalized"])
                key = request_key("rubric", it)
                if (g not in groups and (it.assignment, it.question) not in unread
                        and g not in s["rulings"] and g not in s["groups"]
                        and s["group_status"].get(g) not in TERMINAL and not self._blocked(key, s)):
                    out.append(("rubric", it, key))
                groups.add(g)
            first = self.first_score(it, s)
            key = request_key("verify", it)
            if (first["points"] is not None and (it.crop_id, "verify") not in s["readings"]
                    and not self._no_verify(it, s, first) and not self._blocked(key, s)):
                out.append(("verify", it, key))
        return out

    def _params(self, call_type, it, s, ttl):
        png = (self.cfg.data / it.path).read_bytes()
        api = self.cfg.api
        if call_type == "read":
            return prompts.read_request(api, it.q, png, ttl), png
        r = s["readings"][(it.crop_id, "read")]
        if call_type == "rubric":
            return prompts.rubric_request(api, it.q, self.rubric(it), png, r["reading"], ttl), png
        first = self.first_score(it, s)
        first.update(reading=r["reading"], confidence=r["confidence"])
        rub = self.rubric(it) if it.qtype == "rubric" else None
        return prompts.verify_request(api, it.q, rub, png, first, ttl), png

    def estimate(self, items, s, mode) -> costs.Estimate:
        """Every call the rest of this run could need, assuming no cache hits.
        Rubric calls are counted once per crop, since groups are only known
        after reading."""
        est = costs.Estimate(mode)
        api, ttl = self.cfg.api, ("1h" if mode == "batch" else None)
        placeholder = {"reading": "x" * 200, "confidence": "high"}
        for it in items:
            r = s["readings"].get((it.crop_id, "read"))
            if r is None and not it.needs_read:
                continue
            if r is not None and r["illegible"]:
                continue
            png = (self.cfg.data / it.path).read_bytes()
            todo = []
            if r is None and not self._blocked(request_key("read", it), s):
                todo.append(("read", prompts.read_request(api, it.q, png, ttl), api.est_read_output_tokens))
            reading = r["reading"] if r is not None else placeholder["reading"]
            if it.qtype == "rubric" and (r is None or (it.assignment, it.question, r["normalized"])
                                         not in s["groups"]):
                todo.append(("rubric", prompts.rubric_request(api, it.q, self.rubric(it), png, reading, ttl),
                             api.est_rubric_output_tokens))
            if (it.crop_id, "verify") not in s["readings"] and not (
                    r is not None and self._no_verify(it, s, self.first_score(it, s))):
                rub = self.rubric(it) if it.qtype == "rubric" else None
                first = dict(placeholder, reading=reading, points=it.q["points"],
                             criteria={str(c["id"]): c["points"] for c in rub.criteria} if rub else None)
                todo.append(("verify", prompts.verify_request(api, it.q, rub, png, first, ttl),
                             api.est_verify_output_tokens))
            for call_type, params, n_out in todo:
                est.add(self.cfg.prices, call_type, params["model"], costs.input_tokens(params, png), n_out)
        return est

    # ---- calls -----------------------------------------------------------

    def _classify(self, call_type, message):
        """(status, tool input) for a response."""
        if message.get("stop_reason") == "refusal":
            return "refusal", None
        want = prompts.TOOL_NAMES[call_type]
        block = next((b for b in message.get("content") or []
                      if b.get("type") == "tool_use" and b.get("name") == want), None)
        if block is None:
            return ("truncated" if message.get("stop_reason") == "max_tokens" else "no-tool-call"), None
        return "ok", block["input"]

    def _validate(self, call_type, it, data):
        def need(keys):
            missing = set(keys) - set(data)
            if missing:
                raise ValueError(f"missing {', '.join(sorted(missing))}")
        if call_type in ("read", "verify"):
            need(("reading", "latex", "confidence", "illegible"))
            if data["confidence"] not in ("high", "medium", "low"):
                raise ValueError(f"confidence {data['confidence']!r}")
        if call_type == "verify":
            need(("agrees", "dispute"))
        if call_type == "rubric" or (call_type == "verify" and it.qtype == "rubric"):
            crit = data.get("criteria")
            rub = self.rubric(it)
            ids = {str(c["id"]): float(c["points"]) for c in rub.criteria}
            if not isinstance(crit, dict) or set(crit) != set(ids):
                raise ValueError("criteria do not match the rubric")
            for k, v in crit.items():
                p = v.get("points")
                if not isinstance(p, (int, float)) or not 0 <= p <= ids[k]:
                    raise ValueError(f"criterion {k}: points {p!r}")
        if call_type == "rubric":
            need(("feedback",))

    def apply(self, outcome: Outcome, call_type: str, it: Item, s):
        msg = outcome.message
        status, data, error = "ok", None, outcome.error
        if error:
            status = error.split()[0]
        else:
            status, data = self._classify(call_type, msg)
            if status == "ok":
                try:
                    self._validate(call_type, it, data)
                except ValueError as e:
                    status, error = "invalid", str(e)
        usage = costs.usage_of(msg)
        model = (msg or {}).get("model")
        now = _now()
        c = self.conn
        c.execute("INSERT INTO api_calls (request_key, call_type, crop_id, mode, batch_id, model, status, error, "
                  "input_tokens, output_tokens, cache_write_5m_tokens, cache_write_1h_tokens, cache_read_tokens, "
                  "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (outcome.key, call_type, it.crop_id, outcome.mode, outcome.batch_id, model, status, error,
                   usage["input_tokens"], usage["output_tokens"], usage["cache_write_5m_tokens"],
                   usage["cache_write_1h_tokens"], usage["cache_read_tokens"], now))
        if status != "ok":
            self._attempted.add(outcome.key)
            return status
        if call_type in ("read", "verify"):
            reading = "" if data["illegible"] else data["reading"]
            c.execute("INSERT OR REPLACE INTO readings (crop_id, pass, reading, latex, confidence, illegible, model, "
                      "created_at, request_key, normalized) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (it.crop_id, call_type, reading, data["latex"], data["confidence"], int(data["illegible"]),
                       model, now, outcome.key, checks.normalize(it.qtype, reading)))
        if call_type == "rubric":
            r = s["readings"][(it.crop_id, "read")]
            crit = data["criteria"]
            c.execute("INSERT OR REPLACE INTO judgments (crop_id, pass, assignment, question, normalized_answer, "
                      "points, criteria, feedback, model, request_key, created_at) "
                      "VALUES (?, 'rubric', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (it.crop_id, it.assignment, it.question, r["normalized"],
                       sum(v["points"] for v in crit.values()), json.dumps(crit), data["feedback"], model,
                       outcome.key, now))
        if call_type == "verify":
            crit = data.get("criteria")
            c.execute("INSERT OR REPLACE INTO judgments (crop_id, pass, assignment, question, normalized_answer, "
                      "points, criteria, agrees, dispute, model, request_key, created_at) "
                      "VALUES (?, 'verify', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (it.crop_id, it.assignment, it.question,
                       checks.normalize(it.qtype, "" if data["illegible"] else data["reading"]),
                       sum(v["points"] for v in crit.values()) if crit else None,
                       json.dumps(crit) if crit else None, int(data["agrees"]), data["dispute"], model,
                       outcome.key, now))
        return status

    def _send_standard(self, items, summary):
        """Send every call the run needs through the standard API, up to
        api.concurrency at a time, storing each result as it comes back.
        Whenever a result is stored, the calls it makes possible are planned
        (a crop's verify after its read), so crops move through the passes
        independently. A progress line per pass goes out at least every
        PROGRESS_SECONDS.

        Ctrl-C stops sending: the calls in flight are waited for and stored,
        then KeyboardInterrupt propagates, and a rerun sends only what is
        missing. A second Ctrl-C abandons the calls still in flight. An error
        that stops the run (a bad request, or a 5xx after its retries) does
        the same as the first Ctrl-C, then is raised."""
        api = self.cfg.api
        limiter = standard.Limiter(api.concurrency)
        workers = standard.Workers(api.concurrency, lambda key, params, meta: standard.send(
            self.transport, limiter, key, params, meta, sleep=self.sleep, max_retries=api.max_retries,
            note=workers.note))
        progress = standard.Progress(self.out, self.clock)
        inflight = {}            # request key -> (call type, item)
        error, announced = None, False
        self.out(f"sending through the standard API, up to {limiter.limit} calls at a time")
        for hint in self.on_stop:
            self.out("  " + hint)
        s = self._state()
        with standard.Interrupts() as ctrl_c:
            try:
                while True:
                    plan = [p for p in self.pending(items, s) if p[2] not in inflight]
                    plan.sort(key=lambda p: PASS_ORDER[p[0]])
                    while plan and not (ctrl_c.requested or error) and len(inflight) < limiter.limit:
                        call_type, it, key = plan.pop(0)
                        params, _ = self._params(call_type, it, s, None)
                        inflight[key] = (call_type, it)
                        if call_type == "rubric":
                            self._groups_sent.add(self._group(it, s))
                        workers.submit(key, params, self._meta(call_type, it))
                    remaining = self._remaining(items, s, plan, inflight)
                    progress.update(remaining)
                    if not inflight:
                        break
                    if (ctrl_c.requested or error) and not announced:
                        limiter.stop()  # what hasn't gone out yet stays unsent
                        why = "stopping" if ctrl_c.requested else f"stopping ({error})"
                        self.out(f"{why}: waiting for the {limiter.active} calls already sent"
                                 + (" (Ctrl-C again to abandon them)" if ctrl_c.requested else ""))
                        announced = True
                    got = workers.get(progress.timeout())
                    if got is None:
                        continue
                    kind, key, value = got
                    if kind == "note":
                        self.out(value)
                        continue
                    call_type, it = inflight.pop(key)
                    if kind == "error":
                        error = error or value
                        continue
                    if value is None:  # stopped before it went out
                        continue
                    with self.conn:
                        status = self.apply(value, call_type, it, s)
                    summary.calls[call_type] += 1
                    model = (value.message or {}).get("model")
                    progress.returned(call_type, status == "ok", value.message and costs.dollars(
                        self.cfg.prices, model, "standard", costs.usage_of(value.message)))
                    s = self._state()
            finally:
                limiter.stop()
                workers.close()
        if error is None and not ctrl_c.requested:
            progress.end()
            return
        progress.stopped(remaining)
        if error is not None:
            raise error
        if ctrl_c.requested:
            self._finish(items, summary)
            raise KeyboardInterrupt

    def _group(self, it, s):
        return (it.assignment, it.question, s["readings"][(it.crop_id, "read")]["normalized"])

    def _remaining(self, items, s, plan, inflight) -> dict:
        """Calls of each pass in flight or still to come, as far as is known
        now. Besides the calls planned, a crop whose read is still to come
        will probably need a verify, and a rubric call if it is a rubric item
        (an overcount: its answer may share a group); a rubric crop that has
        been read waits on its group's call, and then needs a verify."""
        out = {p: 0 for p in PASS_ORDER}
        for call_type, _ in inflight.values():
            out[call_type] += 1
        for call_type, _, _ in plan:
            out[call_type] += 1
        planned = {self._group(it, s) for call_type, it, _ in plan if call_type == "rubric"}
        flying = {self._group(it, s) for call_type, it in inflight.values() if call_type == "rubric"}
        waiting = set()
        for it in items:
            r = s["readings"].get((it.crop_id, "read"))
            if r is None:
                if it.needs_read and not self._blocked(request_key("read", it), s):
                    out["verify"] += 1
                    out["rubric"] += it.qtype == "rubric"
                continue
            if r["illegible"] or it.qtype != "rubric":
                continue
            first = self.first_score(it, s)
            if first["points"] is not None or first["reason"] is not None:
                continue
            g = self._group(it, s)
            if g in self._groups_sent and g not in flying:
                continue  # its group's call failed: nothing more for this crop in this run
            if it.crop_id not in s["corrected"]:
                out["verify"] += 1
            if g not in planned and g not in flying:
                waiting.add(g)
        out["rubric"] += len(waiting)
        return out

    def _meta(self, call_type, it):
        return {"call_type": call_type, "crop_id": it.crop_id, "tracking_id": it.tracking_id,
                "assignment": it.assignment, "question": it.question, "type": it.qtype}

    def _send_batch(self, plan, s, summary, by_crop) -> bool:
        reqs = []
        for call_type, it, key in plan:
            params, _ = self._params(call_type, it, s, "1h")
            reqs.append((key, params, self._meta(call_type, it)))
        batch_id = self.transport.batch_submit(reqs)
        with self.conn:
            self.conn.execute("INSERT INTO batches (id, submitted_at, request_keys, status) "
                              "VALUES (?, ?, ?, 'in_progress')",
                              (batch_id, self.clock().isoformat(timespec="seconds"),
                               json.dumps([[k, ct, it.crop_id] for ct, it, k in plan])))
        self.out(f"submitted batch {batch_id}: {len(plan)} requests")
        return self._collect(batch_id, s, summary, by_crop)

    def _collect(self, batch_id, s, summary, by_crop, wait=None) -> bool:
        """Wait for a batch to end and store its results. False when not
        waiting and it hasn't ended yet. A poll line goes out whenever the
        count changes and at least every HEARTBEAT_SECONDS, with the time
        since the batch was submitted."""
        wait = self.wait if wait is None else wait
        row = self.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        wanted = {k: (ct, cid) for k, ct, cid in json.loads(row["request_keys"])}
        submitted = dt.datetime.fromisoformat(row["submitted_at"])
        last, last_at = None, None
        while True:
            st = self.transport.batch_status(batch_id)
            if st["status"] == "ended":
                break
            counts = st.get("counts") or {}
            done = sum(v for k, v in counts.items() if k != "processing")
            line = f"batch {batch_id}: {st['status']}, {done} of {len(wanted)} done"
            now = self.clock()
            if line != last or not wait or (now - last_at).total_seconds() >= HEARTBEAT_SECONDS:
                self.out(f"{line}, {elapsed((now - submitted).total_seconds())}")
                last, last_at = line, now
                if wait and not self._hinted:
                    for hint in self.on_wait:
                        self.out("  " + hint)
                    self._hinted = True
            if not wait:
                summary.batch_pending = batch_id
                return False
            self.sleep(min(self.cfg.api.batch_poll_seconds, HEARTBEAT_SECONDS))
        seen = set()
        with self.conn:
            for outcome in self.transport.batch_results(batch_id):
                if outcome.key not in wanted or outcome.key in seen:
                    continue
                seen.add(outcome.key)
                call_type, crop_id = wanted[outcome.key]
                it = by_crop.get(crop_id)
                if it is None:  # crop no longer in scope (another assignment): still record it
                    it = next(i for i in self.items() if i.crop_id == crop_id)
                self.apply(outcome, call_type, it, s)
                summary.calls[call_type] += 1
            self._attempted.update(set(wanted) - seen)  # no result: send again on the next run
            self.conn.execute("UPDATE batches SET status = CASE status WHEN 'canceling' THEN 'canceled' "
                              "ELSE 'collected' END, collected_at = ? WHERE id = ?", (_now(), batch_id))
        return True

    def cancel_batches(self) -> list[dict]:
        """grading batch cancel: cancel every batch still running, wait for
        each to end, and store the results that came in before the cancel.
        The requests that never ran are recorded as canceled, which is not
        terminal, so the next run sends exactly those (--now: through the
        standard API). A batch that had already ended is just collected."""
        items = self.items()
        by_crop = {it.crop_id: it for it in items}
        rows = self.conn.execute("SELECT id, status FROM batches WHERE status IN ('in_progress', 'canceling') "
                                 "ORDER BY submitted_at").fetchall()
        for row in rows:
            if row["status"] == "in_progress" and self.transport.batch_status(row["id"])["status"] != "ended":
                self.transport.batch_cancel(row["id"])
                with self.conn:
                    self.conn.execute("UPDATE batches SET status = 'canceling', canceled_at = ? WHERE id = ?",
                                      (_now(), row["id"]))
                self.out(f"canceling batch {row['id']}")
        out = []
        for row in rows:
            self._collect(row["id"], self._state(), Summary(), by_crop, wait=True)
            status = self.conn.execute("SELECT status FROM batches WHERE id = ?", (row["id"],)).fetchone()[0]
            got = {r[0]: r[1] for r in self.conn.execute(
                "SELECT status, count(*) FROM api_calls WHERE batch_id = ? GROUP BY status", (row["id"],))}
            out.append({"id": row["id"], "status": status, "canceled": got.pop("canceled", 0),
                        "results": sum(got.values())})
        self._finish(items, Summary())
        return out

    # ---- the run ---------------------------------------------------------

    def run(self, assignment=None) -> Summary:
        items = self.items(assignment)
        by_crop = {it.crop_id: it for it in items}
        summary = Summary()

        now_only = self.force_mode == "standard"
        for row in self.conn.execute("SELECT id, status FROM batches WHERE status IN ('in_progress', 'canceling') "
                                     "ORDER BY submitted_at").fetchall():
            self.out(f"resuming batch {row['id']}")
            summary.mode = "batch"
            # --now never waits on the batch queue: a batch that has ended is
            # collected, one that hasn't stops the run.
            if not self._collect(row["id"], self._state(), summary, by_crop, wait=self.wait and not now_only):
                self._finish(items, summary)
                if now_only:
                    raise BatchPending(row["id"], row["status"])
                return summary
            # Requests canceled before this run never ran: send them now.
            self._attempted -= {r[0] for r in self.conn.execute(
                "SELECT request_key FROM api_calls WHERE batch_id = ? AND status = 'canceled'", (row["id"],))}

        s = self._state()
        todo = [it for it in items if self._needs_work(it, s)]
        mode = self.force_mode or ("batch" if len(todo) > self.cfg.api.batch_threshold else "standard")
        if not self.transport.supports_batch:
            mode = "standard"
        summary.mode = mode if todo else summary.mode
        if todo and self.pending(items, s):
            est = self.estimate(items, s, mode)
            summary.estimate = est
            if self.transport.live:
                other = None
                if self.transport.supports_batch:
                    other = self.estimate(items, s, "standard" if mode == "batch" else "batch")
                self._guard(est, len(todo), other)

        if mode == "standard" and self.pending(items, self._state()):
            self._send_standard(items, summary)
        while mode == "batch":
            s = self._state()
            plan = self.pending(items, s)
            if not plan:
                break
            if not self._send_batch(plan, s, summary, by_crop):
                break
            if self._canceled_while_waiting():
                break
        summary = self._finish(items, summary)
        self.transport.finish(summary.waiting == 0 and summary.batch_pending is None)
        return summary

    def _canceled_while_waiting(self) -> bool:
        """`grading batch cancel` ran while this run waited on its batch: stop
        rather than submit the next round."""
        row = self.conn.execute("SELECT id, status FROM batches ORDER BY submitted_at DESC, rowid DESC LIMIT 1"
                                ).fetchone()
        if row["status"] != "canceled":
            return False
        self.out(f"batch {row['id']} was canceled; stopping here")
        return True

    def _needs_work(self, it, s):
        r = s["readings"].get((it.crop_id, "read"))
        if r is None:
            return it.needs_read and not self._blocked(request_key("read", it), s)
        return (not r["illegible"] and (it.crop_id, "verify") not in s["readings"]
                and not self._no_verify(it, s, self.first_score(it, s)))

    def _guard(self, est, n_crops, other=None):
        """other: the same run through the other API, priced for comparison."""
        api = self.cfg.api
        how = {"batch": "the Batch API at half price: most batches finish within an hour, "
                        "but one can take up to 24 hours",
               "standard": "the standard API at full price: results as each call returns"}
        self.out(f"{n_crops} crops to grade through {how[est.mode]}. Estimated (no cache hits assumed):")
        for line in est.lines():
            self.out("  " + line)
        if other is not None:
            flag, name = ("--now", "standard API") if est.mode == "batch" else ("--batch", "Batch API")
            self.out(f"  with {flag} ({name}): about ${other.total_usd:.2f}")
        if est.total_usd > api.max_run_usd:
            raise CostRefused(f"estimated ${est.total_usd:.2f} is above api.max_run_usd (${api.max_run_usd:.2f}); "
                              "grade fewer crops at a time or raise the ceiling")
        api_name = "Batch API" if est.mode == "batch" else "standard API"
        if self.confirm is None or not self.confirm(
                f"Send {est.total_calls} calls through the {api_name}, about ${est.total_usd:.2f}?"):
            raise Cancelled("cancelled; nothing was sent")

    # ---- outcome ---------------------------------------------------------

    def decide(self, it: Item, s) -> dict | None:
        """The proposed score and review routing for one crop, or None while a
        call for it is still to come."""
        base = {"points": None, "source": None, "feedback": None, "reasons": [], "detail": {}}
        if not it.needs_read:
            base.update(points=0.0, source="code")
            if it.blank_confirmed != 1:
                base["reasons"].append("blank")
            return base
        r = s["readings"].get((it.crop_id, "read"))
        if r is None:
            st = s["failed"].get(request_key("read", it))
            if st in TERMINAL:
                base["reasons"].append("read-failed")
                base["detail"]["read_status"] = st
                return base
            return None
        first = self.first_score(it, s)
        detail = {"read": {"reading": r["reading"], "latex": r["latex"], "confidence": r["confidence"],
                           "model": r["model"]}}
        if first.get("criteria_detail"):
            detail["criteria"] = first["criteria_detail"]
        settled = dict(reading=r["reading"], normalized=r["normalized"], detail=detail, verify_points=None,
                       verify_reading=None)
        if it.crop_id in s["corrected"]:
            detail["corrected_from"] = r["original"]
            if first["points"] is None:
                # A rubric answer fixed by hand with no ruling and no rubric
                # score for it yet: review scores it (or the next run's call).
                base.update(settled, reasons=["reading-corrected"])
                return base
            reasons = ["rubric"] if first.get("source") == "rubric" else []
            base.update(settled, points=first["points"], source=first.get("source"),
                        feedback=first.get("feedback"), reasons=reasons)
            return base
        if first.get("source") == "ruling":
            base.update(settled, points=first["points"], source="ruling", feedback=first.get("feedback"))
            return base
        reasons = []
        if first["points"] is None:
            if first["reason"] is None:
                return None  # rubric call still to come
            reasons.append(first["reason"])
        if r["confidence"] != "high" and not r["illegible"]:
            reasons.append("low-confidence")
        v = s["readings"].get((it.crop_id, "verify"))
        vj = s["judgments"].get((it.crop_id, "verify"))
        verify_points = None
        if first["points"] is not None:
            if v is None:
                st = s["failed"].get(request_key("verify", it))
                if st not in TERMINAL:
                    return None
                reasons.append("verify-failed")
                detail["verify_status"] = st
            else:
                detail["verify"] = {"reading": v["reading"], "confidence": v["confidence"],
                                    "agrees": bool(vj["agrees"]), "dispute": vj["dispute"], "model": v["model"]}
                if it.qtype == "rubric":
                    vcrit = json.loads(vj["criteria"]) if vj["criteria"] else {}
                    detail["verify"]["criteria"] = vcrit
                    verify_points = vj["points"]
                    same = (vj["agrees"] and not v["illegible"]
                            and (first.get("criteria") is None
                                 or {k: x["points"] for k, x in vcrit.items()} == first["criteria"]))
                else:
                    same = (vj["agrees"] and not v["illegible"] and v["confidence"] == "high"
                            and v["normalized"] == r["normalized"])
                if not same:
                    reasons.append("verify-disagreement")
        if it.qtype == "rubric" and first.get("source") == "rubric":
            reasons.append("rubric")
        base.update(points=first["points"], source=first.get("source"), feedback=first.get("feedback"),
                    reasons=reasons, detail=detail, verify_points=verify_points,
                    verify_reading=v["reading"] if v is not None else None, reading=r["reading"],
                    normalized=r["normalized"])
        return base

    def settle(self, assignment=None) -> Summary:
        """Recompute scores and the queue from what is stored, sending nothing:
        what review calls after each decision."""
        return self._finish(self.items(assignment), Summary())

    def _finish(self, items, summary) -> Summary:
        s = self._state()
        now = _now()
        decided = {r[0] for r in self.conn.execute("SELECT crop_id FROM scores WHERE source = 'review'")}
        with self.conn:
            for it in items:
                if it.crop_id in decided:
                    continue
                d = self.decide(it, s)
                if d is None:
                    summary.waiting += 1
                    continue
                status = "review" if d["reasons"] else "final"
                d["detail"].update(reasons=d["reasons"], crop=it.path)
                self.conn.execute(
                    "INSERT INTO scores (tracking_id, assignment, question, points, max_points, source, feedback, "
                    "scored_at, crop_id, normalized_answer, status, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (tracking_id, assignment, question) DO UPDATE SET points = excluded.points, "
                    "max_points = excluded.max_points, source = excluded.source, feedback = excluded.feedback, "
                    "scored_at = excluded.scored_at, crop_id = excluded.crop_id, "
                    "normalized_answer = excluded.normalized_answer, status = excluded.status, "
                    "detail = excluded.detail WHERE scores.source IS NOT 'review'",
                    (it.tracking_id, it.assignment, it.question, d["points"], it.q["points"], d["source"],
                     d["feedback"], now, it.crop_id, d.get("normalized"), status, json.dumps(d["detail"])))
                if status == "final":
                    summary.final += 1
                    self.conn.execute("UPDATE review_queue SET resolved_at = ?, resolution = 'settled' "
                                      "WHERE crop_id = ? AND resolved_at IS NULL", (now, it.crop_id))
                    continue
                summary.in_review += 1
                for reason in d["reasons"]:
                    summary.review[reason] = summary.review.get(reason, 0) + 1
                self.conn.execute(
                    "INSERT INTO review_queue (crop_id, tracking_id, assignment, question, type, reasons, priority, "
                    "normalized_answer, reading, verify_reading, points, verify_points, max_points, feedback, detail, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (crop_id) DO UPDATE SET reasons = excluded.reasons, priority = excluded.priority, "
                    "normalized_answer = excluded.normalized_answer, reading = excluded.reading, "
                    "verify_reading = excluded.verify_reading, points = excluded.points, "
                    "verify_points = excluded.verify_points, feedback = excluded.feedback, "
                    "detail = excluded.detail, created_at = excluded.created_at, resolved_at = NULL, "
                    "resolution = NULL",
                    (it.crop_id, it.tracking_id, it.assignment, it.question, it.qtype, json.dumps(d["reasons"]),
                     min(PRIORITY[x] for x in d["reasons"]), d.get("normalized"), d.get("reading"),
                     d.get("verify_reading"), d["points"], d.get("verify_points"), it.q["points"], d["feedback"],
                     json.dumps(d["detail"]), now))
        return summary


def ask(prompt: str) -> bool:
    """Interactive confirmation; refuses when there is no terminal to ask on."""
    if not sys.stdin.isatty():
        print(f"{prompt} (no terminal to confirm on: pass --yes to proceed)")
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
