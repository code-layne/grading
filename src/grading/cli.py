"""grading [--config FILE] ingest [--received YYYY-MM-DD] [PDF ...]
grading [--config FILE] status
grading [--config FILE] read [--assignment ID] [--record DIR | --replay DIR] [--yes] [--no-wait]
grading [--config FILE] eval [--set DIR] [--workdir DIR] [--record DIR | --replay DIR | --fake] [--yes]
grading [--config FILE] review [--port N] [--no-browser]
grading [--config FILE] report [--assignment ID ...] [--all] [--partial]"""

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import config, costs, db, evaluate, report
from .apiclient import AnthropicTransport, ApiError, RecordingTransport, ReplayTransport
from .ingest import ingest_file, ingest_inbox
from .manifests import ManifestError, ManifestIndex
from .pipeline import Cancelled, CostRefused, Pipeline, ask
from .report import ReportError
from .review import ReviewError
from .roster import RosterError
from .rubrics import RubricError


def _report(results):
    for r in results:
        if r.skipped:
            print(f"{r.path.name}: already ingested, skipped")
            continue
        print(f"{r.path.name}: {len(r.pages)} pages, {r.count('placed')} placed, "
              f"{r.count('duplicate')} duplicate, {r.count('unplaced')} unplaced")
        for p in r.pages:
            if p.status != "placed":
                where = f" ({p.tracking_id} {p.assignment} p{p.page})" if p.tracking_id else ""
                print(f"  page {p.scan_page}: {p.status}, {p.reason}{where}")


def cmd_ingest(cfg, args):
    received = dt.date.fromisoformat(args.received) if args.received else None
    if args.pdf:
        conn = db.connect(cfg.database)
        manifests = ManifestIndex(cfg.curricula)
        try:
            results = [ingest_file(cfg, Path(p), conn, manifests, received) for p in args.pdf]
        finally:
            conn.close()
    else:
        if not cfg.inbox.is_dir():
            print(f"grading: inbox {cfg.inbox} does not exist", file=sys.stderr)
            return 1
        results = ingest_inbox(cfg, received)
        if not results:
            print(f"no PDFs in {cfg.inbox}")
    _report(results)
    return 0


def cmd_status(cfg, args):
    if not cfg.database.exists():
        print(f"nothing ingested yet ({cfg.database} does not exist)")
        return 0
    conn = db.connect(cfg.database)
    try:
        for row in conn.execute("SELECT status, count(*) AS n FROM pages GROUP BY status ORDER BY status"):
            print(f"{row['status']:10} {row['n']}")
        blanks = conn.execute("SELECT count(*) FROM crops WHERE blank = 1 AND blank_confirmed IS NULL").fetchone()[0]
        print(f"{'blank':10} {blanks} crops to confirm")
        scored = conn.execute("SELECT count(*) FROM scores WHERE status = 'final'").fetchone()[0]
        review = conn.execute("SELECT count(*) FROM review_queue WHERE resolved_at IS NULL").fetchone()[0]
        print(f"{'scored':10} {scored} final, {review} to review")
        for b in conn.execute("SELECT id, submitted_at FROM batches WHERE status = 'in_progress'"):
            print(f"batch {b['id']} submitted {b['submitted_at']} not collected: run `grading read` to collect it")
        rows = conn.execute(
            "SELECT p.id, s.filename, p.scan_page, p.reason, p.detail FROM pages p JOIN scans s ON s.id = p.scan_id "
            "WHERE p.status = 'unplaced' AND p.resolved_at IS NULL ORDER BY p.id").fetchall()
        if rows:
            print("unplaced:")
            for r in rows:
                print(f"  #{r['id']} {r['filename']} page {r['scan_page']}: {r['reason']} {r['detail'] or ''}")
    finally:
        conn.close()
    return 0


def _transport(cfg, args, *, record_in_repo: bool):
    if args.replay:
        return ReplayTransport(Path(args.replay).expanduser())
    rec = Path(args.record).expanduser() if args.record else None
    if rec is not None and not record_in_repo:
        tree = config.git_work_tree(rec)
        if tree is not None:
            raise config.ConfigError(f"--record {rec} is inside the git work tree {tree}: a recording holds "
                                     "student work, keep it outside every repository")
    if getattr(args, "fake", False):
        from .fake import FakeTransport
        transport = FakeTransport(Path(args.set) / "expected-scores.csv")
    else:
        transport = AnthropicTransport(cfg.api)
    return RecordingTransport(transport, rec) if rec is not None else transport


def _confirm(args):
    return (lambda prompt: True) if args.yes else ask


def _summary(s, conn, cfg, first_call_id):
    made = ", ".join(f"{t} {n}" for t, n in s.calls.items() if n)
    print(f"calls: {made or 'none'} ({s.mode} API)")
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(s.review.items(), key=lambda kv: -kv[1]))
    print(f"scored: {s.final} final, {s.in_review} to review" + (f" ({reasons})" if reasons else ""))
    if s.waiting:
        print(f"waiting: {s.waiting} items still need a call; rerun `grading read` to finish them")
    if s.batch_pending:
        print(f"batch {s.batch_pending} is still running; rerun `grading read` to collect it")
    if any(s.calls.values()):
        for line in costs.spent_lines(costs.spent(conn, cfg.prices, first_call_id)):
            print("  " + line)


def cmd_read(cfg, args):
    transport = _transport(cfg, args, record_in_repo=False)
    conn = db.connect(cfg.database)
    try:
        first_call_id = conn.execute("SELECT coalesce(max(id), 0) FROM api_calls").fetchone()[0]
        pipeline = Pipeline(cfg, conn, transport, confirm=_confirm(args), wait=not args.no_wait)
        summary = pipeline.run(args.assignment)
        _summary(summary, conn, cfg, first_call_id)
        if getattr(transport, "stale", None):
            print(f"{len(transport.stale)} replayed requests differ from the recording (prompt or crop changed)")
    finally:
        conn.close()
    return 0


def cmd_eval(cfg, args):
    # A recording of the synthetic set may become a test fixture; one of any
    # other set holds real student work and stays out of repositories.
    synthetic = Path(args.set).resolve() == evaluate.DEFAULT_SET.resolve()
    transport = _transport(cfg, args, record_in_repo=synthetic)
    workdir = Path(args.workdir).expanduser() if args.workdir else (
        cfg.data / "eval" / dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    resuming = (workdir / "data" / "grading.sqlite").exists()
    print(f"eval: {Path(args.set)} {'resumed in' if resuming else 'into'} {workdir}")
    report = evaluate.run(cfg, transport, Path(args.set), workdir, confirm=_confirm(args))
    print("\n".join(evaluate.lines(report)))
    print(f"\nreport: {workdir / 'eval-report.json'}")
    if args.record:
        print(f"recording: {Path(args.record).expanduser()} (replay with `grading eval --replay {args.record}`)")
    return 0


def cmd_review(cfg, args):
    from .webapp import serve
    if not cfg.database.exists():
        print(f"nothing ingested yet ({cfg.database} does not exist)")
        return 0
    serve(cfg, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_report(cfg, args):
    if not cfg.database.exists():
        print(f"nothing ingested yet ({cfg.database} does not exist)")
        return 1
    conn = db.connect(cfg.database)
    try:
        out = report.run(cfg, conn, assignments=args.assignment, everything=args.all, partial=args.partial)
    finally:
        conn.close()
    rep = out["report"]
    # Tracking ids and counts only: the names stay in the report folder.
    print(f"report: {out['folder']}")
    for f in out["files"]:
        print(f"  {f.name}")
    listed = sum(len(s.students) for s in rep.sections)
    print(f"{len(rep.run)} submissions, {len(rep.assignments)} assignments "
          f"({', '.join(a['assignment'] for a in rep.assignments)}), {listed} students listed")
    for a, b in rep.blocked.items():
        print(f"partial: {a} has {b['review']} items in review and {b['ungraded']} not graded yet; "
              "they are not counted")
    if rep.unknown:
        print(f"not in roster-map.csv: {', '.join(rep.unknown)}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="grading")
    ap.add_argument("--config", help="config file (default $GRADING_CONFIG, then ~/.config/grading/config.toml)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="ingest scanned PDFs (default: everything in the inbox)")
    p.add_argument("pdf", nargs="*", help="scan PDFs to ingest instead of the inbox; they are not moved")
    p.add_argument("--received", help="date the papers came in (default today)")
    sub.add_parser("status", help="page counts, review counts and the unplaced queue")

    def calls(p):
        g = p.add_mutually_exclusive_group()
        g.add_argument("--record", metavar="DIR", help="save every request and response to DIR")
        g.add_argument("--replay", metavar="DIR", help="answer every request from a recording: no network")
        p.add_argument("--yes", action="store_true", help="skip the cost confirmation (the ceiling still applies)")

    p = sub.add_parser("read", help="read, score and verify every crop not graded yet")
    p.add_argument("--assignment", help="only this assignment id")
    p.add_argument("--no-wait", action="store_true",
                   help="submit a batch and exit; rerun to collect it")
    calls(p)
    p = sub.add_parser("eval", help="grade the synthetic class set and compare with expected-scores.csv")
    p.add_argument("--set", default=str(evaluate.DEFAULT_SET),
                   help="class set folder: class-set-scan.pdf, expected-scores.csv, curriculum/")
    p.add_argument("--workdir", help="where its data folder goes (default <data>/eval/<date-time>); "
                   "an earlier eval's workdir resumes it")
    p.add_argument("--fake", action="store_true",
                   help="answer from expected-scores.csv with injected faults instead of the API")
    calls(p)
    p = sub.add_parser("review", help="the review queue and the unplaced pages, on localhost")
    p.add_argument("--port", type=int, default=5005)
    p.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    p = sub.add_parser("report", help="scores, feedback sheets and the summary, with names (reads the roster)")
    p.add_argument("--assignment", action="append", help="only this assignment (repeatable)")
    p.add_argument("--all", action="store_true", help="every submission, including ones already reported")
    p.add_argument("--partial", action="store_true",
                   help="report even while items are in review; they are left out and marked")
    args = ap.parse_args(argv)
    if getattr(args, "fake", False) and args.replay:
        ap.error("--fake and --replay both say where answers come from; pick one")
    try:
        cfg = config.load(args.config)
        return {"ingest": cmd_ingest, "status": cmd_status, "read": cmd_read, "eval": cmd_eval,
                "review": cmd_review, "report": cmd_report}[args.command](cfg, args)
    except (config.ConfigError, ManifestError, RubricError, ApiError, CostRefused, RosterError,
            ReportError, ReviewError) as e:
        print(f"grading: {e}", file=sys.stderr)
        return 1
    except Cancelled as e:
        print(f"grading: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
