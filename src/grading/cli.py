"""grading [--config FILE] ingest [--received YYYY-MM-DD] [PDF ...]
grading [--config FILE] status"""

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import config, db
from .ingest import ingest_file, ingest_inbox
from .manifests import ManifestError, ManifestIndex


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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="grading")
    ap.add_argument("--config", help="config file (default $GRADING_CONFIG, then ~/.config/grading/config.toml)")
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ingest", help="ingest scanned PDFs (default: everything in the inbox)")
    p.add_argument("pdf", nargs="*", help="scan PDFs to ingest instead of the inbox; they are not moved")
    p.add_argument("--received", help="date the papers came in (default today)")
    sub.add_parser("status", help="page counts and the unplaced queue")
    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.config)
        return {"ingest": cmd_ingest, "status": cmd_status}[args.command](cfg, args)
    except (config.ConfigError, ManifestError) as e:
        print(f"grading: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
