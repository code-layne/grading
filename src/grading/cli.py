"""grading [--config FILE] ingest [--received YYYY-MM-DD] [PDF ...]
grading [--config FILE] status
grading [--config FILE] read [--assignment ID] [--now | --batch] [--record DIR | --replay DIR] [--yes] [--no-wait]
grading [--config FILE] eval [--set DIR] [--workdir DIR] [--now | --batch] [--record DIR | --replay DIR | --fake] [--yes]
grading [--config FILE] batch cancel [--workdir DIR]
grading [--config FILE] review [--port N] [--no-browser]
grading [--config FILE] report [--assignment ID ...] [--all] [--partial]"""

import argparse
import dataclasses
import datetime as dt
import shlex
import sys
from pathlib import Path

from . import config, costs, db, evaluate, report
from .apiclient import AnthropicTransport, ApiError, RecordingTransport, ReplayTransport
from .ingest import ingest_file, ingest_inbox
from .manifests import ManifestError, ManifestIndex
from .pipeline import BatchPending, Cancelled, CostRefused, Pipeline, ask, elapsed
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
        for b in conn.execute("SELECT id, submitted_at, status FROM batches "
                              "WHERE status IN ('in_progress', 'canceling') ORDER BY submitted_at"):
            age = elapsed((dt.datetime.now() - dt.datetime.fromisoformat(b["submitted_at"])).total_seconds())
            if b["status"] == "canceling":
                print(f"batch {b['id']} canceling, submitted {age} ago: `grading batch cancel` finishes it")
            else:
                print(f"batch {b['id']} submitted {age} ago, not collected: `grading read` collects it, "
                      "or `grading batch cancel` and then `grading read --now`")
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


def _force_mode(args):
    return "standard" if args.now else "batch" if args.batch else None


def _command(args, *words, now=False):
    """The command line that picks this run up again: the same options,
    with --now in place of --batch when now is set."""
    cmd = ["grading"] + (["--config", args.config] if args.config else []) + list(words)
    if getattr(args, "assignment", None):
        cmd += ["--assignment", args.assignment]
    cmd += ["--now"] if now else ["--batch"] if args.batch else []
    for flag in ("record", "replay"):
        if getattr(args, flag, None):
            cmd += [f"--{flag}", getattr(args, flag)]
    if getattr(args, "fake", False):
        cmd.append("--fake")
    if args.yes:
        cmd.append("--yes")
    return shlex.join(cmd)


def _waiting_hints(args, *words, workdir=None):
    where = ["--workdir", str(workdir)] if workdir is not None else []
    resume = _command(args, *words, *where)
    cancel = shlex.join(["grading"] + (["--config", args.config] if args.config else []) + ["batch", "cancel"]
                        + where)
    return [f"Ctrl-C is safe: the batch keeps running without this terminal, and `{resume}` picks it up.",
            f"To stop waiting: Ctrl-C, `{cancel}`, then `{_command(args, *words, *where, now=True)}`."], cancel


def _stop_hints(args, *words, workdir=None):
    where = ["--workdir", str(workdir)] if workdir is not None else []
    return [f"Ctrl-C stops cleanly: what came back is kept, and `{_command(args, *words, *where, now=args.now)}` "
            "sends the rest."]


def _pending(e, cancel, now_cmd, wait_cmd):
    if e.status == "canceling":
        return f"{e}: `{cancel}` waits for it to end and stores what it finished, then rerun `{now_cmd}`"
    return (f"{e}, and --now would pay for its requests twice. `{cancel}` stops it and keeps what it "
            f"finished; then `{now_cmd}` sends the rest. Or `{wait_cmd}` waits for it.")


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
    hints, cancel = _waiting_hints(args, "read")
    try:
        first_call_id = conn.execute("SELECT coalesce(max(id), 0) FROM api_calls").fetchone()[0]
        pipeline = Pipeline(cfg, conn, transport, confirm=_confirm(args), wait=not args.no_wait,
                            force_mode=_force_mode(args), on_wait=hints, on_stop=_stop_hints(args, "read"))
        try:
            summary = pipeline.run(args.assignment)
        except BatchPending as e:
            print(f"grading: {_pending(e, cancel, _command(args, 'read', now=True), _command(args, 'read'))}",
                  file=sys.stderr)
            return 1
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
    words = ["eval"] + ([] if Path(args.set).resolve() == evaluate.DEFAULT_SET.resolve() else ["--set", args.set])
    hints, cancel = _waiting_hints(args, *words, workdir=workdir)
    try:
        report = evaluate.run(cfg, transport, Path(args.set), workdir, confirm=_confirm(args),
                              force_mode=_force_mode(args), on_wait=hints,
                              on_stop=_stop_hints(args, *words, workdir=workdir))
    except BatchPending as e:
        where = ["--workdir", str(workdir)]
        now_cmd, wait_cmd = _command(args, *words, *where, now=True), _command(args, *words, *where)
        print(f"grading: {_pending(e, cancel, now_cmd, wait_cmd)}", file=sys.stderr)
        return 1
    print("\n".join(evaluate.lines(report)))
    print(f"\nreport: {workdir / 'eval-report.json'}")
    if args.record:
        print(f"recording: {Path(args.record).expanduser()} (replay with `grading eval --replay {args.record}`)")
    return 0


def cmd_batch(cfg, args):
    if args.workdir:
        cfg = dataclasses.replace(cfg, data=Path(args.workdir).expanduser() / "data")
    where = ["--workdir", args.workdir] if args.workdir else []
    if not cfg.database.exists():
        print(f"nothing to cancel ({cfg.database} does not exist)")
        return 1
    conn = db.connect(cfg.database)
    try:
        if not conn.execute("SELECT count(*) FROM batches WHERE status IN ('in_progress', 'canceling')"
                            ).fetchone()[0]:
            print(f"no batch running in {cfg.data}")
            return 0
        cmd = shlex.join(["grading"] + (["--config", args.config] if args.config else []) + ["batch", "cancel"]
                         + where)
        pipeline = Pipeline(cfg, conn, AnthropicTransport(cfg.api), on_wait=[
            f"Ctrl-C is safe: the cancel stands, and `{cmd}` finishes collecting it."])
        done = pipeline.cancel_batches()
    finally:
        conn.close()
    rerun = shlex.join(["grading"] + (["--config", args.config] if args.config else [])
                       + (["eval", "--workdir", args.workdir] if args.workdir else ["read"]) + ["--now"])
    for b in done:
        if b["status"] == "canceled":
            print(f"batch {b['id']} canceled: {b['results']} results stored, {b['canceled']} requests never ran")
        else:
            print(f"batch {b['id']} had already ended: {b['results']} results stored")
    if any(b["canceled"] for b in done):
        print(f"`{rerun}` sends the requests that never ran through the standard API")
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
        g = p.add_mutually_exclusive_group()
        g.add_argument("--now", action="store_true",
                       help="send everything through the standard API, at full price, whatever the run's size")
        g.add_argument("--batch", action="store_true",
                       help="send everything through the Batch API, at half price, even a small run")

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
    p = sub.add_parser("batch", help="the Batch API batches of the data folder")
    bsub = p.add_subparsers(dest="batch_command", required=True)
    p = bsub.add_parser("cancel", help="cancel the running batch and keep the results it has; "
                        "then `grading read --now` sends the rest")
    p.add_argument("--workdir", help="an eval's workdir instead of the data folder")
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
                "batch": cmd_batch, "review": cmd_review, "report": cmd_report}[args.command](cfg, args)
    except (config.ConfigError, ManifestError, RubricError, ApiError, CostRefused, RosterError,
            ReportError, ReviewError) as e:
        print(f"grading: {e}", file=sys.stderr)
        return 1
    except Cancelled as e:
        print(f"grading: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        # Everything is stored as it arrives and a batch id as soon as it is
        # submitted, so rerunning the command carries on from here.
        print("\ngrading: interrupted; rerun the same command to carry on", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
