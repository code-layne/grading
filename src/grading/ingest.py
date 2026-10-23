"""Ingest: scanned PDFs in, aligned and strip-blanked pages and answer-box
crops out, recorded in the database by tracking id.

For each page of a scan:
  1. render at the configured dpi (300)
  2. decode the QR code: assignment | page | tracking id
  3. find the four registration marks and fit the transform to the manifest
  4. warp the page onto the manifest's layout and blank the strip (the
     tracking id; on packets printed before cover sheets, the name)
  5. crop each answer box with a small margin, threshold it, and mark it
     blank when it holds almost no ink

A cover sheet (the one sheet with a student's name) is skipped: nothing of it
is stored but its page number in the scan and the reason, in
scans.skipped_pages. A page that can't be placed goes to the unplaced queue
(stored strip-blanked too). A second copy of a page already placed for the
same student and assignment is stored and flagged as a duplicate; it never
replaces the first.
"""

import datetime as dt
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from . import db, scan
from .manifests import ManifestIndex


@dataclass
class PageResult:
    scan_page: int
    status: str
    reason: str | None = None
    tracking_id: str | None = None
    assignment: str | None = None
    page: int | None = None
    page_id: int | None = None


@dataclass
class ScanResult:
    path: Path
    skipped: bool = False
    pages: list[PageResult] = field(default_factory=list)

    def count(self, status):
        return sum(p.status == status for p in self.pages)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_png(data_dir: Path, rel: str, image) -> str:
    out = data_dir / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(out), image):
        raise OSError(f"could not write {out}")
    return rel


def _record_assignment(conn, manifest, now):
    if conn.execute("SELECT 1 FROM assignments WHERE assignment = ?", (manifest.assignment,)).fetchone():
        return
    d = manifest.data
    conn.execute("INSERT INTO assignments VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (manifest.assignment, d.get("course"), d["pages"], str(manifest.path),
                  manifest.sha256, json.dumps(d), now))
    for q in d["questions"]:
        conn.execute("INSERT INTO questions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (manifest.assignment, q["id"], q["type"], q["points"], q["page"],
                      json.dumps(q["box"]), q.get("answer"), q.get("check"),
                      q.get("tolerance"), q.get("rubric")))


def ingest_file(cfg, pdf: Path, conn=None, manifests=None, received_on: dt.date | None = None) -> ScanResult:
    pdf = Path(pdf)
    own = conn is None
    conn = conn or db.connect(cfg.database)
    manifests = manifests or ManifestIndex(cfg.curricula)
    settings = cfg.ingest
    received = (received_on or dt.date.today()).isoformat()
    now = dt.datetime.now().isoformat(timespec="seconds")
    sha = _sha256(pdf)
    result = ScanResult(pdf)
    try:
        if conn.execute("SELECT 1 FROM scans WHERE sha256 = ?", (sha,)).fetchone():
            result.skipped = True
            return result
        with conn:  # one transaction per scan
            scan_id = conn.execute(
                "INSERT INTO scans (sha256, filename, page_count, ingested_at, received_on) "
                "VALUES (?, ?, ?, ?, ?)", (sha, pdf.name, scan.page_count(pdf), now, received)).lastrowid
            # One page in memory at a time, so a cover sheet's image is gone
            # before the next page is rendered.
            for n, gray in enumerate(scan.render_pages(pdf, settings.dpi), start=1):
                result.pages.append(_ingest_page(cfg, conn, manifests, scan_id, sha, n, gray, received, now))
                del gray
            skipped = [{"scan_page": p.scan_page, "reason": p.reason} for p in result.pages if p.status == "skipped"]
            if skipped:
                conn.execute("UPDATE scans SET skipped_pages = ? WHERE id = ?", (json.dumps(skipped), scan_id))
    finally:
        if own:
            conn.close()
    return result


def _ingest_page(cfg, conn, manifests, scan_id, sha, n, gray, received, now) -> PageResult:
    settings, data = cfg.ingest, cfg.data
    page_rel = f"pages/{sha[:12]}/{n:03d}.png"
    try:
        placed = scan.place_page(gray, manifests, settings)
    except scan.CoverPage:
        return PageResult(n, "skipped", reason="cover")
    except scan.PlacementError as e:
        _write_png(data, page_rel, scan.unplaced_image(gray, settings))
        page_id = conn.execute(
            "INSERT INTO pages (scan_id, scan_page, status, reason, detail, received_on, image_path) "
            "VALUES (?, ?, 'unplaced', ?, ?, ?, ?)",
            (scan_id, n, e.reason, e.detail, received, page_rel)).lastrowid
        return PageResult(n, "unplaced", reason=e.reason, page_id=page_id)

    code = placed.code
    manifest = manifests.get(code.assignment)
    _record_assignment(conn, manifest, now)
    first = conn.execute(
        "SELECT id FROM pages WHERE status = 'placed' AND tracking_id = ? AND assignment = ? AND page = ?",
        (code.tracking_id, code.assignment, code.page)).fetchone()
    status = "duplicate" if first else "placed"
    canonical = placed.canonical
    _write_png(data, page_rel, scan.clean(canonical, settings.ink_threshold))
    page_id = conn.execute(
        "INSERT INTO pages (scan_id, scan_page, status, reason, tracking_id, assignment, page, duplicate_of, "
        "received_on, transform, mark_residual, rejected_codes, image_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (scan_id, n, status, "duplicate-page" if first else None, code.tracking_id, code.assignment,
         code.page, first["id"] if first else None, received,
         json.dumps([[round(v, 6) for v in row] for row in placed.transform.tolist()]),
         round(placed.residual_px, 3),
         json.dumps([c.text for c in placed.rejected_codes]) if placed.rejected_codes else None,
         page_rel)).lastrowid

    _record_crops(cfg, conn, manifest, code.tracking_id, code.page, canonical, page_id, bool(first), received)
    return PageResult(n, status, reason="duplicate-page" if first else None, tracking_id=code.tracking_id,
                      assignment=code.assignment, page=code.page, page_id=page_id)


def _record_crops(cfg, conn, manifest, tracking_id, page, canonical, page_id, duplicate, received):
    """Crop every answer box on an aligned page and record the submission."""
    settings = cfg.ingest
    suffix = f"-dup{page_id}" if duplicate else ""
    for q in manifest.questions_on(page):
        crop, ink, border = scan.crop_box(canonical, q["box"], manifest.data, settings)
        rel = _write_png(cfg.data, f"crops/{manifest.assignment}/{tracking_id}/{q['id']}{suffix}.png", crop)
        conn.execute(
            "INSERT INTO crops (page_id, tracking_id, assignment, question, path, border_px, ink_px, blank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (page_id, tracking_id, manifest.assignment, q["id"], rel, json.dumps(list(map(int, border))),
             ink, int(ink < settings.blank_ink_px)))
    conn.execute(
        "INSERT INTO submissions VALUES (?, ?, ?, ?) ON CONFLICT (tracking_id, assignment) DO UPDATE SET "
        "first_received_on = min(first_received_on, excluded.first_received_on), "
        "last_received_on = max(last_received_on, excluded.last_received_on)",
        (tracking_id, manifest.assignment, received, received))


# The alphabet roster_import.py draws tracking ids from: no 0, O, 1, I or L.
TRACKING_ID = re.compile(r"^[ABCDEFGHJKMNPQRSTUVWXYZ2-9]{6}$")


class PlaceError(Exception):
    pass


def place_by_hand(cfg, conn, page_id: int, assignment: str, page: int, tracking_id: str,
                  manifests: ManifestIndex | None = None) -> PageResult:
    """Place an unplaced page on an assignment, page and student chosen in
    review. Its stored image (already strip-blanked) is aligned to the
    assignment's manifest by its registration marks and cropped like any
    other page; a page whose marks can't be found has to be rescanned."""
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
    if row is None or row["status"] != "unplaced" or row["resolved_at"] is not None:
        raise PlaceError(f"page #{page_id} is not waiting in the unplaced queue")
    tracking_id = tracking_id.strip().upper()
    if not TRACKING_ID.match(tracking_id):
        raise PlaceError(f"{tracking_id!r} is not a tracking id (six characters, no 0, O, 1, I or L)")
    manifest = (manifests or ManifestIndex(cfg.curricula)).get(assignment)
    if manifest is None:
        raise PlaceError(f"no manifest for assignment {assignment!r}")
    if not 1 <= int(page) <= manifest.pages:
        raise PlaceError(f"{assignment} has pages 1 to {manifest.pages}")
    page = int(page)
    settings = cfg.ingest
    stored = cv2.imread(str(cfg.data / row["image_path"]), cv2.IMREAD_GRAYSCALE)
    if stored is None:
        raise PlaceError(f"page image {row['image_path']} is missing")
    try:
        M, resid = scan.align(stored, manifest.data, settings.dpi, settings.ink_threshold,
                              settings.max_mark_residual_px)
    except scan.PlacementError as e:
        raise PlaceError(f"can't align this page ({e}); rescan it") from None
    canonical = scan.warp_to_canonical(stored, M, manifest.data, settings.dpi)
    scan.blank_strip(canonical, manifest.data, settings.dpi)
    now = dt.datetime.now().isoformat(timespec="seconds")
    with conn:
        _record_assignment(conn, manifest, now)
        first = conn.execute(
            "SELECT id FROM pages WHERE status = 'placed' AND tracking_id = ? AND assignment = ? AND page = ?",
            (tracking_id, assignment, page)).fetchone()
        status = "duplicate" if first else "placed"
        _write_png(cfg.data, row["image_path"], scan.clean(canonical, settings.ink_threshold))
        conn.execute(
            "UPDATE pages SET status = ?, detail = ?, tracking_id = ?, assignment = ?, page = ?, duplicate_of = ?, "
            "transform = ?, mark_residual = ?, resolved_at = ? WHERE id = ?",
            (status, f"placed by hand in review ({row['reason']})", tracking_id, assignment, page,
             first["id"] if first else None,
             json.dumps([[round(v, 6) for v in r] for r in M.tolist()]), round(resid, 3), now, page_id))
        _record_crops(cfg, conn, manifest, tracking_id, page, canonical, page_id, bool(first), row["received_on"])
    return PageResult(row["scan_page"], status, reason="duplicate-page" if first else None,
                      tracking_id=tracking_id, assignment=assignment, page=page, page_id=page_id)


def dismiss_page(conn, page_id: int, why: str = "dismissed in review"):
    """Take a page out of the unplaced queue without placing it (a stray
    sheet, a master copy). It stays in the database."""
    with conn:
        n = conn.execute("UPDATE pages SET resolved_at = ?, detail = ? WHERE id = ? AND status = 'unplaced' "
                         "AND resolved_at IS NULL",
                         (dt.datetime.now().isoformat(timespec="seconds"), why, page_id)).rowcount
    if not n:
        raise PlaceError(f"page #{page_id} is not waiting in the unplaced queue")


def ingest_inbox(cfg, received_on: dt.date | None = None) -> list[ScanResult]:
    """Ingest every PDF in the inbox, then move each into inbox/ingested/."""
    done = cfg.inbox / "ingested"
    results = []
    conn = db.connect(cfg.database)
    manifests = ManifestIndex(cfg.curricula)
    try:
        for pdf in sorted(p for p in cfg.inbox.iterdir() if p.suffix.lower() == ".pdf" and p.is_file()):
            results.append(ingest_file(cfg, pdf, conn, manifests, received_on))
            done.mkdir(exist_ok=True)
            target = done / pdf.name
            if target.exists():
                target = done / f"{pdf.stem}-{_sha256(pdf)[:8]}{pdf.suffix}"
            shutil.move(pdf, target)
    finally:
        conn.close()
    return results
