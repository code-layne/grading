"""Review: step 8 of the design's pipeline, without the web page.

The queue is review_queue's open rows, grouped by question and then by
normalized answer, in three tiers:

  check   disagreements, no score (illegible, unparseable, a failed call)
          and low-confidence readings
  rubric  rubric scores, which all come to review in this phase
  blanks  boxes ingest found empty, as one batch to confirm

A decision is stored as one of the pipeline's inputs, then Pipeline.settle
recomputes the scores and the queue from them, so what review shows, what
the next `grading read` does and what the report sees always agree:

  approve        a ruling for (assignment, question, normalized answer):
                 points and feedback, as proposed or as changed. It scores
                 every matching answer now and in every later run, late
                 papers included, without coming back to review.
  fix reading    a correction for one crop; the crop is then scored (and
                 grouped) by the corrected answer
  confirm blank  crops.blank_confirmed; a box with writing goes back to
                 `grading read`
  decide         a score for one crop alone, for answers with nothing to
                 rule on (illegible, or a call that failed)

Students appear only by tracking id: this module never reads the roster.
"""

import datetime as dt
import json
import re

from . import checks, rubrics
from .pipeline import Pipeline

TIERS = ("check", "rubric", "blanks")
TIER_TITLES = {"check": "Disagreements and low confidence", "rubric": "Rubric items", "blanks": "Blanks to confirm"}


class ReviewError(Exception):
    pass


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def _tier(priority):
    return "check" if priority <= 2 else "rubric" if priority == 3 else "blanks"


class Review:
    def __init__(self, cfg, conn):
        self.cfg, self.conn = cfg, conn
        self._rubrics = {}

    # ---- the queue -------------------------------------------------------

    def _open_rows(self):
        return self.conn.execute(
            "SELECT r.*, c.path, c.blank FROM review_queue r JOIN crops c ON c.id = r.crop_id "
            "WHERE r.resolved_at IS NULL ORDER BY r.assignment, r.question, r.id").fetchall()

    def groups(self) -> list[dict]:
        """Every open group, in review order. A group's key is
        [assignment, question, normalized answer], or ["blanks"] for the batch."""
        groups, blanks = {}, []
        for r in self._open_rows():
            reasons = json.loads(r["reasons"])
            if reasons == ["blank"]:
                blanks.append(r)
                continue
            k = (r["assignment"], r["question"], r["normalized_answer"])
            g = groups.setdefault(k, {"key": list(k), "rows": [], "priority": r["priority"]})
            g["rows"].append(r)
            g["priority"] = min(g["priority"], r["priority"])
        out = []
        for (a, q, norm), g in groups.items():
            reasons = sorted({x for row in g["rows"] for x in json.loads(row["reasons"])})
            first = g["rows"][0]
            out.append({"key": g["key"], "tier": _tier(g["priority"]), "priority": g["priority"],
                        "assignment": a, "question": q, "type": first["type"], "count": len(g["rows"]),
                        "reasons": reasons, "reading": first["reading"],
                        "individual": not norm})
        out.sort(key=lambda g: (TIERS.index(g["tier"]), g["assignment"], _qorder(g["question"]),
                                g["priority"], -g["count"], g["key"][2] or ""))
        if blanks:
            out.append({"key": ["blanks"], "tier": "blanks", "priority": 4, "assignment": None,
                        "question": None, "type": None, "count": len(blanks), "reasons": ["blank"],
                        "reading": None, "individual": True})
        return out

    def group(self, key) -> dict | None:
        """One group with everything review shows for it."""
        rows = self._open_rows()
        if list(key) == ["blanks"]:
            members = [r for r in rows if json.loads(r["reasons"]) == ["blank"]]
            if not members:
                return None
            return {"key": ["blanks"], "tier": "blanks", "count": len(members), "individual": False,
                    "items": [{"crop_id": r["crop_id"], "tracking_id": r["tracking_id"],
                               "assignment": r["assignment"], "question": r["question"]} for r in members]}
        a, q, norm = key
        members = [r for r in rows if (r["assignment"], r["question"], r["normalized_answer"]) == (a, q, norm)
                   and json.loads(r["reasons"]) != ["blank"]]
        if not members:
            return None
        question = self.question(a, q)
        items = []
        for r in members:
            detail = json.loads(r["detail"] or "{}")
            items.append({
                "crop_id": r["crop_id"], "tracking_id": r["tracking_id"], "reasons": json.loads(r["reasons"]),
                "reading": r["reading"], "verify_reading": r["verify_reading"], "points": r["points"],
                "verify_points": r["verify_points"], "feedback": r["feedback"],
                "confidence": detail.get("read", {}).get("confidence"),
                "criteria": detail.get("criteria"), "verify": detail.get("verify"),
                "corrected_from": detail.get("corrected_from"),
                "read_status": detail.get("read_status") or detail.get("verify_status")})
        proposed = next((i["points"] for i in items if i["points"] is not None), None)
        feedback = next((i["feedback"] for i in items if i["feedback"]), "")
        ruling = self.conn.execute("SELECT * FROM rulings WHERE assignment = ? AND question = ? "
                                   "AND normalized_answer = ?", (a, q, norm)).fetchone()
        matching = self.conn.execute("SELECT count(*) FROM scores WHERE assignment = ? AND question = ? "
                                     "AND normalized_answer = ?", (a, q, norm)).fetchone()[0]
        reasons = sorted({x for i in items for x in i["reasons"]})
        return {"key": [a, q, norm], "tier": _tier(min(r["priority"] for r in members)), "count": len(items),
                "assignment": a, "question": q, "q": question, "items": items, "reasons": reasons,
                "proposed": proposed, "feedback": feedback, "normalized": norm, "individual": not norm,
                "ruling": dict(ruling) if ruling else None, "matching": matching}

    def question(self, assignment, question) -> dict:
        row = self.conn.execute(
            "SELECT q.*, a.manifest_path FROM questions q JOIN assignments a USING (assignment) "
            "WHERE q.assignment = ? AND q.question = ?", (assignment, question)).fetchone()
        if row is None:
            raise ReviewError(f"no question {assignment} {question}")
        out = {"id": question, "type": row["type"], "points": row["points"], "answer": row["answer"],
               "check": row["check_kind"], "tolerance": row["tolerance"], "rubric": None}
        if row["type"] == "rubric" and row["rubric"]:
            k = (row["manifest_path"], row["rubric"])
            if k not in self._rubrics:
                try:
                    self._rubrics[k] = rubrics.load(row["manifest_path"], row["rubric"]).data
                except rubrics.RubricError:
                    self._rubrics[k] = None
            out["rubric"] = self._rubrics[k]
        return out

    def counts(self) -> dict:
        c = self.conn
        return {"items": c.execute("SELECT count(*) FROM review_queue WHERE resolved_at IS NULL").fetchone()[0],
                "unplaced": c.execute("SELECT count(*) FROM pages WHERE status = 'unplaced' "
                                      "AND resolved_at IS NULL").fetchone()[0],
                "rulings": c.execute("SELECT count(*) FROM rulings").fetchone()[0]}

    def crop_path(self, crop_id):
        row = self.conn.execute("SELECT path FROM crops WHERE id = ?", (crop_id,)).fetchone()
        return None if row is None else self.cfg.data / row["path"]

    def page_path(self, page_id):
        row = self.conn.execute("SELECT image_path FROM pages WHERE id = ?", (page_id,)).fetchone()
        return None if row is None else self.cfg.data / row["image_path"]

    # ---- decisions -------------------------------------------------------

    def _settle(self, assignment):
        Pipeline(self.cfg, self.conn, None, out=lambda *a: None).settle(assignment)

    def _points(self, value, max_points):
        try:
            p = float(value)
        except (TypeError, ValueError):
            raise ReviewError(f"{value!r} is not a score") from None
        if not 0 <= p <= max_points:
            raise ReviewError(f"a score must be from 0 to {max_points:g}")
        return p

    def approve(self, key, points=None, feedback=None) -> int:
        """Save a ruling for the group's answer (its proposed score and
        feedback unless changed) and apply it. Returns how many answers it
        scores."""
        a, q, norm = key
        g = self.group(key)
        if g is None:
            raise ReviewError("that group is no longer in the queue")
        if g["individual"]:
            raise ReviewError("there is no answer to rule on here: score each crop on its own")
        if points is None or points == "":
            points = g["proposed"]
            if points is None:
                raise ReviewError("nothing was proposed for this answer: type a score")
        points = self._points(points, g["q"]["points"])
        feedback = g["feedback"] if feedback is None else feedback.strip()
        with self.conn:
            self.conn.execute(
                "INSERT INTO rulings (assignment, question, normalized_answer, points, feedback, decided_at, reading) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (assignment, question, normalized_answer) DO UPDATE SET "
                "points = excluded.points, feedback = excluded.feedback, decided_at = excluded.decided_at, "
                "reading = excluded.reading",
                (a, q, norm, points, feedback or None, _now(), g["items"][0]["reading"]))
        self._settle(a)
        return self.conn.execute("SELECT count(*) FROM scores WHERE assignment = ? AND question = ? "
                                 "AND normalized_answer = ? AND source = 'ruling'", (a, q, norm)).fetchone()[0]

    def decide(self, crop_id, points, feedback=None):
        """A score for this crop alone: no ruling, nothing else changes."""
        row = self._open(crop_id)
        points = self._points(points, row["max_points"])
        now = _now()
        with self.conn:
            self.conn.execute("UPDATE scores SET points = ?, feedback = ?, source = 'review', status = 'final', "
                              "scored_at = ? WHERE crop_id = ?",
                              (points, (feedback or "").strip() or None, now, crop_id))
            self.conn.execute("UPDATE review_queue SET resolved_at = ?, resolution = 'decided' WHERE crop_id = ?",
                              (now, crop_id))

    def correct(self, crop_id, reading) -> list:
        """Fix one crop's reading. Returns the group key it lands in, or None
        when that settled it."""
        row = self._open(crop_id)
        reading = (reading or "").strip()
        if not reading:
            raise ReviewError("type the reading (an empty box is a blank, not a reading)")
        norm = checks.normalize(row["type"], reading)
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO corrections VALUES (?, ?, ?, ?)",
                              (crop_id, reading, norm, _now()))
        self._settle(row["assignment"])
        still = self.conn.execute("SELECT 1 FROM review_queue WHERE crop_id = ? AND resolved_at IS NULL",
                                  (crop_id,)).fetchone()
        return [row["assignment"], row["question"], norm] if still else None

    def confirm_blanks(self, crop_ids, not_blank=()):
        """Confirm a batch of blanks (they score zero). A crop in not_blank
        holds writing after all: it goes back to `grading read`."""
        not_blank = set(not_blank)
        open_blanks = {r["crop_id"]: r for r in self._open_rows() if json.loads(r["reasons"]) == ["blank"]}
        unknown = (set(crop_ids) | not_blank) - set(open_blanks)
        if unknown:
            raise ReviewError(f"not blanks waiting in the queue: {sorted(unknown)}")
        with self.conn:
            for cid in set(crop_ids) - not_blank:
                self.conn.execute("UPDATE crops SET blank_confirmed = 1 WHERE id = ?", (cid,))
            for cid in not_blank:
                self.conn.execute("UPDATE crops SET blank_confirmed = 0 WHERE id = ?", (cid,))
                self.conn.execute("DELETE FROM review_queue WHERE crop_id = ?", (cid,))
                self.conn.execute("UPDATE scores SET points = NULL, source = NULL, status = 'review', "
                                  "feedback = NULL WHERE crop_id = ?", (cid,))
        for a in {open_blanks[c]["assignment"] for c in set(crop_ids) | not_blank}:
            self._settle(a)

    def _open(self, crop_id):
        row = self.conn.execute("SELECT * FROM review_queue WHERE crop_id = ? AND resolved_at IS NULL",
                                (crop_id,)).fetchone()
        if row is None:
            raise ReviewError(f"crop {crop_id} is not waiting in the review queue")
        return row

    # ---- unplaced pages --------------------------------------------------

    def unplaced(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT p.id, p.scan_id, p.scan_page, p.reason, p.detail, p.received_on, s.ingested_at, s.page_count "
            "FROM pages p JOIN scans s ON s.id = p.scan_id WHERE p.status = 'unplaced' AND p.resolved_at IS NULL "
            "ORDER BY p.id").fetchall()
        # The scan's file name is left out on purpose: it is whatever the
        # copier or the teacher called it, and could hold a name.
        return [dict(r) for r in rows]

    def assignments(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT assignment, pages FROM assignments ORDER BY assignment")]

    def candidates(self, assignment, page) -> dict:
        """Tracking ids to suggest when placing a page by hand: who has other
        pages of this assignment but not this one, then every id seen."""
        missing = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT tracking_id FROM pages WHERE status = 'placed' AND assignment = ? AND tracking_id NOT IN "
            "(SELECT tracking_id FROM pages WHERE status = 'placed' AND assignment = ? AND page = ?) "
            "ORDER BY tracking_id", (assignment, assignment, page))]
        known = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT tracking_id FROM pages WHERE tracking_id IS NOT NULL ORDER BY tracking_id")]
        return {"missing_this_page": missing, "known": known}


def _qorder(question):
    """gs2 before gs10."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", question or "")]
