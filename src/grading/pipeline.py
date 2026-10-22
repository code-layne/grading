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

The work runs in rounds: each round sends every call whose inputs exist
(round 1 the reads, round 2 the rubric calls and the choice and short
verifies, round 3 the rubric verifies). A run over more than
api.batch_threshold crops sends each round as one Batch API batch; the batch
id is stored, so a rerun after an interruption collects that batch instead
of submitting it again. Everything is stored as it arrives, so a rerun only
sends what is still missing.
"""

import datetime as dt
import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field

from . import checks, costs, prompts, rubrics
from .apiclient import Outcome
from .manifests import ManifestError

TERMINAL = {"refusal", "truncated", "no-tool-call", "invalid"}  # retrying won't help
PRIORITY = {"verify-disagreement": 0, "illegible": 1, "unparseable": 1, "not-a-letter": 1, "not-a-number": 1,
            "read-failed": 1, "rubric-failed": 1, "verify-failed": 1, "low-confidence": 2, "rubric": 3,
            "blank": 4}


class CostRefused(Exception):
    pass


class Cancelled(Exception):
    pass


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
                 force_mode: str | None = None):
        self.cfg, self.conn, self.transport = cfg, conn, transport
        self.confirm = confirm
        self.wait = wait
        self.out = out
        self.sleep = sleep
        self.force_mode = force_mode
        self._rubrics = {}
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
            try:
                chk = checks.score(item.q, r["reading"])
            except ValueError as e:  # the key itself is the problem
                raise ManifestError(f"{item.assignment} {item.question}: {e}") from None
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
        for it in items:
            r = s["readings"].get((it.crop_id, "read"))
            if r is None:
                key = request_key("read", it)
                if it.needs_read and not self._blocked(key, s):
                    out.append(("read", it, key))
                continue
            if r["illegible"]:
                continue
            if it.qtype == "rubric":
                # One call per group of identical answers, made on its first crop.
                g = (it.assignment, it.question, r["normalized"])
                key = request_key("rubric", it)
                if (g not in groups and g not in s["rulings"] and g not in s["groups"]
                        and s["group_status"].get(g) not in TERMINAL and not self._blocked(key, s)):
                    out.append(("rubric", it, key))
                groups.add(g)
            first = self.first_score(it, s)
            key = request_key("verify", it)
            if (first["points"] is not None and (it.crop_id, "verify") not in s["readings"]
                    and not self._blocked(key, s)):
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
            if (it.crop_id, "verify") not in s["readings"]:
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

    def _send_standard(self, plan, s, summary):
        for call_type, it, key in plan:
            params, _ = self._params(call_type, it, s, None)
            meta = self._meta(call_type, it)
            outcome = self.transport.create(key, params, meta)
            with self.conn:
                self.apply(outcome, call_type, it, s)
            summary.calls[call_type] += 1

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
                              (batch_id, _now(), json.dumps([[k, ct, it.crop_id] for ct, it, k in plan])))
        self.out(f"submitted batch {batch_id}: {len(plan)} requests")
        return self._collect(batch_id, s, summary, by_crop)

    def _collect(self, batch_id, s, summary, by_crop) -> bool:
        """Wait for a batch to end and store its results. False when not
        waiting and it hasn't ended yet."""
        row = self.conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        wanted = {k: (ct, cid) for k, ct, cid in json.loads(row["request_keys"])}
        last = None
        while True:
            st = self.transport.batch_status(batch_id)
            if st["status"] == "ended":
                break
            counts = st.get("counts") or {}
            done = sum(v for k, v in counts.items() if k != "processing")
            line = f"batch {batch_id}: {st['status']}, {done} of {len(wanted)} done"
            if line != last or not self.wait:
                self.out(line)
                last = line
            if not self.wait:
                summary.batch_pending = batch_id
                return False
            self.sleep(self.cfg.api.batch_poll_seconds)
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
            self.conn.execute("UPDATE batches SET status = 'collected', collected_at = ? WHERE id = ?",
                              (_now(), batch_id))
        return True

    # ---- the run ---------------------------------------------------------

    def run(self, assignment=None) -> Summary:
        items = self.items(assignment)
        by_crop = {it.crop_id: it for it in items}
        summary = Summary()

        for row in self.conn.execute("SELECT id FROM batches WHERE status = 'in_progress' ORDER BY submitted_at"
                                     ).fetchall():
            self.out(f"resuming batch {row['id']}")
            summary.mode = "batch"
            if not self._collect(row["id"], self._state(), summary, by_crop):
                return self._finish(items, summary)

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
                self._guard(est, len(todo))

        while True:
            s = self._state()
            plan = self.pending(items, s)
            if not plan:
                break
            if mode == "batch":
                if not self._send_batch(plan, s, summary, by_crop):
                    break
            else:
                self._send_standard(plan, s, summary)
        return self._finish(items, summary)

    def _needs_work(self, it, s):
        r = s["readings"].get((it.crop_id, "read"))
        if r is None:
            return it.needs_read and not self._blocked(request_key("read", it), s)
        return not r["illegible"] and (it.crop_id, "verify") not in s["readings"]

    def _guard(self, est, n_crops):
        api = self.cfg.api
        self.out(f"{n_crops} crops to grade, {est.mode} API, estimated (no cache hits assumed):")
        for line in est.lines():
            self.out("  " + line)
        if est.total_usd > api.max_run_usd:
            raise CostRefused(f"estimated ${est.total_usd:.2f} is above api.max_run_usd (${api.max_run_usd:.2f}); "
                              "grade fewer crops at a time or raise the ceiling")
        if self.confirm is None or not self.confirm(f"Send {est.total_calls} calls, about ${est.total_usd:.2f}?"):
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

    def _finish(self, items, summary) -> Summary:
        s = self._state()
        now = _now()
        with self.conn:
            for it in items:
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
                    self.conn.execute("DELETE FROM review_queue WHERE crop_id = ? AND resolved_at IS NULL",
                                      (it.crop_id,))
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
                    "detail = excluded.detail, created_at = excluded.created_at "
                    "WHERE review_queue.resolved_at IS NULL",
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
