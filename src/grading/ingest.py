"""Ingest: scanned PDFs in, aligned and strip-blanked pages and answer-box
crops out, recorded in the database by tracking id.

For each page of a scan:
  1. render at the configured dpi (300)
  2. decode the QR code: assignment | page | tracking id
  3. find the four registration marks and fit the transform to the manifest
  4. warp the page onto the manifest's layout and blank the name strip
  5. crop each answer box with a small margin, threshold it, and mark it
     blank when it holds almost no ink

A page that can't be placed goes to the unplaced queue (stored strip-blanked
too). A second copy of a page already placed for the same student and
assignment is stored and flagged as a duplicate; it never replaces the first.
"""

import datetime as dt
import hashlib
import json
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
            pages = list(scan.render_pages(pdf, settings.dpi))
            scan_id = conn.execute(
                "INSERT INTO scans (sha256, filename, page_count, ingested_at, received_on) "
                "VALUES (?, ?, ?, ?, ?)", (sha, pdf.name, len(pages), now, received)).lastrowid
            for n, gray in enumerate(pages, start=1):
                result.pages.append(_ingest_page(cfg, conn, manifests, scan_id, sha, n, gray, received, now))
    finally:
        if own:
            conn.close()
    return result


def _ingest_page(cfg, conn, manifests, scan_id, sha, n, gray, received, now) -> PageResult:
    settings, data = cfg.ingest, cfg.data
    page_rel = f"pages/{sha[:12]}/{n:03d}.png"
    try:
        placed = scan.place_page(gray, manifests, settings)
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

    suffix = f"-dup{page_id}" if first else ""
    for q in manifest.questions_on(code.page):
        crop, ink, border = scan.crop_box(canonical, q["box"], manifest.data, settings)
        rel = _write_png(data, f"crops/{code.assignment}/{code.tracking_id}/{q['id']}{suffix}.png", crop)
        conn.execute(
            "INSERT INTO crops (page_id, tracking_id, assignment, question, path, border_px, ink_px, blank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (page_id, code.tracking_id, code.assignment, q["id"], rel, json.dumps(list(map(int, border))),
             ink, int(ink < settings.blank_ink_px)))
    conn.execute(
        "INSERT INTO submissions VALUES (?, ?, ?, ?) ON CONFLICT (tracking_id, assignment) DO UPDATE SET "
        "first_received_on = min(first_received_on, excluded.first_received_on), "
        "last_received_on = max(last_received_on, excluded.last_received_on)",
        (code.tracking_id, code.assignment, received, received))
    return PageResult(n, status, reason="duplicate-page" if first else None, tracking_id=code.tracking_id,
                      assignment=code.assignment, page=code.page, page_id=page_id)


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
