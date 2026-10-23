"""grading eval: the whole pipeline on the synthetic class set, compared with
the scores it should get.

The class set (tests/fixtures/synthetic, from algebra_2's graded_sample
eval) is ingested into a fresh data folder of its own, then read, scored
and verified exactly as `grading read` would, and every item's proposed
score is compared with expected-scores.csv. The real database is never
touched.

An item agrees when its proposed score equals the expected points. An item
with no proposed score (illegible, unparseable) is counted apart: it went to
review, which is the right outcome, but it is not an agreement.
"""

import csv
import dataclasses
import json
from pathlib import Path

from . import costs, db
from .ingest import ingest_file
from .pipeline import Pipeline

REPO = Path(__file__).resolve().parents[2]
DEFAULT_SET = REPO / "tests" / "fixtures" / "synthetic"
TYPES = ("choice", "short", "rubric")


def eval_config(cfg, set_dir: Path, workdir: Path):
    """cfg with the class set's curriculum and a data folder of its own."""
    return dataclasses.replace(cfg, curricula=[set_dir / "curriculum"], data=workdir / "data",
                               inbox=workdir / "inbox", roster=workdir / "roster-map.csv")


def run(cfg, transport, set_dir: Path = DEFAULT_SET, workdir: Path | None = None, *, confirm=None,
        out=print, force_mode=None, on_wait=(), on_stop=()) -> dict:
    set_dir = Path(set_dir)
    ecfg = eval_config(cfg, set_dir, Path(workdir))
    scan = set_dir / "class-set-scan.pdf"
    conn = db.connect(ecfg.database)
    try:
        ingest_file(ecfg, scan, conn)
        summary = Pipeline(ecfg, conn, transport, confirm=confirm, out=out, force_mode=force_mode,
                           on_wait=on_wait, on_stop=on_stop).run()
        report = compare(ecfg, conn, set_dir / "expected-scores.csv")
        report["mode"] = summary.mode
        report["waiting"] = summary.waiting
        report["stale"] = list(getattr(transport, "stale", []))
        (Path(workdir) / "eval-report.json").write_text(json.dumps(report, indent=1))
        return report
    finally:
        conn.close()


def compare(cfg, conn, expected_csv: Path) -> dict:
    with open(expected_csv, newline="") as f:
        expected = {(r["tracking_id"], r["item"]): r for r in csv.DictReader(f)}
    rows = {(r["tracking_id"], r["question"]): r for r in conn.execute(
        "SELECT s.*, c.path FROM scores s JOIN crops c ON c.id = s.crop_id")}
    queue = {r["crop_id"]: r for r in conn.execute("SELECT * FROM review_queue WHERE resolved_at IS NULL")}

    by_type = {t: {"items": 0, "agree": 0, "disagree": 0, "no_score": 0, "missing": 0, "review": 0} for t in TYPES}
    disagreements, review = [], {}
    for (tid, item), exp in sorted(expected.items()):
        t = by_type[exp["type"]]
        t["items"] += 1
        got = rows.get((tid, item))
        if got is None:
            t["missing"] += 1
            disagreements.append({"tracking_id": tid, "item": item, "type": exp["type"], "expected": float(exp["points"]),
                                  "points": None, "problem": "not scored", "crop": None})
            continue
        q = queue.get(got["crop_id"])
        if q is not None:
            t["review"] += 1
            for reason in json.loads(q["reasons"]):
                review[reason] = review.get(reason, 0) + 1
        detail = json.loads(got["detail"] or "{}")
        if got["points"] is None:
            t["no_score"] += 1
        elif abs(got["points"] - float(exp["points"])) < 1e-9:
            t["agree"] += 1
        else:
            t["disagree"] += 1
            disagreements.append({
                "tracking_id": tid, "item": item, "type": exp["type"], "expected": float(exp["points"]),
                "points": got["points"], "reading": detail.get("read", {}).get("reading"),
                "response": exp["response"], "crop": str(cfg.data / got["path"]),
                "in_review": q is not None})
    return {"by_type": by_type, "disagreements": disagreements, "review": review,
            "review_items": len(queue), "spent": costs.spent(conn, cfg.prices), "data": str(cfg.data)}


def lines(report: dict) -> list[str]:
    out = ["Agreement with expected-scores.csv:",
           f"  {'type':7} {'items':>5} {'agree':>6} {'disagree':>8} {'no score':>8} {'review':>6}  agreement"]
    for t in TYPES:
        r = report["by_type"][t]
        scored = r["items"] - r["missing"]
        pct = 100.0 * r["agree"] / r["items"] if r["items"] else 0.0
        out.append(f"  {t:7} {r['items']:>5} {r['agree']:>6} {r['disagree']:>8} {r['no_score']:>8} "
                   f"{r['review']:>6}  {pct:5.1f}%" + (f"  ({r['missing']} not scored)" if scored < r["items"] else ""))
    out.append("")
    if report["disagreements"]:
        out.append("Disagreements:")
        for d in report["disagreements"]:
            if d["points"] is None:
                out.append(f"  {d['tracking_id']} {d['item']}: {d['problem']}")
                continue
            flag = "" if d["in_review"] else "  NOT IN REVIEW"
            out.append(f"  {d['tracking_id']} {d['item']} ({d['type']}): {d['points']:g} for {d['expected']:g}, "
                       f"read {d['reading']!r}, wrote {d['response']!r}{flag}")
            out.append(f"    {d['crop']}")
    else:
        out.append("Disagreements: none")
    out.append("")
    out.append(f"Review: {report['review_items']} items")
    for reason, n in sorted(report["review"].items(), key=lambda kv: -kv[1]):
        out.append(f"  {reason:20} {n}")
    if report.get("waiting"):
        out.append(f"  ({report['waiting']} items still waiting on a call: rerun to finish)")
    out.append("")
    out.append("Tokens and dollars by call type:")
    out.extend("  " + line for line in costs.spent_lines(report["spent"]))
    if report.get("stale"):
        out.append("")
        out.append(f"{len(report['stale'])} replayed requests differ from the recording (prompt or crop changed); "
                   "their responses are from the old request.")
    return out
