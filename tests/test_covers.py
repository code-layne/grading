"""A cover sheet (the one sheet with the student's name) that went through
the scanner: nothing of it is stored but its scan page number and the reason."""

import contextlib
import io
import json
import sqlite3

import cv2
import numpy as np
import pymupdf
import pytest
import zxingcpp

from grading import scan
from grading.cli import main

from conftest import SYNTHETIC, gray_to_pdf_page, read_csv

NAME = "Wilhelmina Quarrington-Vasquez"  # invented
COVER_TEXT = "COVER|alg2-u3-l1-gs|462HAN"
COVER_AT = 4  # the cover goes in as scan page 4 of 13


def cover_pdf(path, text=COVER_TEXT):
    """A cover as math-shared prints it: the name, and a COVER code in the
    same position and size as a graded page's QR code, with no marks."""
    if hasattr(zxingcpp, "create_barcode"):  # zxing-cpp 2.3 on
        code = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.QRCode)
        qr = np.array(zxingcpp.write_barcode_to_image(code, scale=16, add_quiet_zones=False))
    else:
        qr = np.array(zxingcpp.write_barcode(zxingcpp.BarcodeFormat.QRCode, text, 400, 400, 0))
    ok, png = cv2.imencode(".png", qr)
    assert ok
    x, y, w, h = scan.DEFAULT_LAYOUT["qr"]
    paper_h = scan.DEFAULT_LAYOUT["paper"][1]
    with pymupdf.open() as doc:
        page = doc.new_page(width=612, height=792)
        page.insert_text((72, 200), NAME, fontsize=28)
        page.insert_text((72, 240), "Algebra 2, Unit 3 Lesson 1", fontsize=14)
        page.insert_image(pymupdf.Rect(x, paper_h - y - h, x + w, paper_h - y), stream=png.tobytes())
        doc.save(path)
    with pymupdf.open(path) as doc:
        assert NAME in doc[0].get_text()


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("covers")
    cover = tmp / "cover.pdf"
    cover_pdf(cover)
    (gray,) = scan.render_pages(cover, 300)
    src = SYNTHETIC / "class-set-scan.pdf"
    pdf = tmp / "stack.pdf"
    with pymupdf.open(src) as orig, pymupdf.open() as doc:
        doc.insert_pdf(orig, to_page=COVER_AT - 2)
        gray_to_pdf_page(doc, gray)
        doc.insert_pdf(orig, from_page=COVER_AT - 1)
        doc.save(pdf)
    cover.unlink()

    root = tmp / "grading"
    root.mkdir()
    cfg = root / "config.toml"
    cfg.write_text(f'[paths]\ncurricula = ["{SYNTHETIC / "curriculum"}"]\ninbox = "inbox"\ndata = "data"\n'
                   'roster = "roster-map.csv"\n')
    out = {}
    for cmd in (["ingest", str(pdf)], ["status"]):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["--config", str(cfg), *cmd]) == 0
        out[cmd[0]] = buf.getvalue()
    return root / "data", out


WORDS = [w.lower() for w in (NAME, *NAME.replace("-", " ").split(), "COVER|")]


def test_a_cover_code_raises_cover_page(tmp_path):
    cover_pdf(tmp_path / "cover.pdf")
    (gray,) = scan.render_pages(tmp_path / "cover.pdf", 300)
    with pytest.raises(scan.CoverPage):
        scan.decode_codes(gray, 160)
    with pytest.raises(scan.CoverPage):  # fed upside down
        scan.decode_codes(cv2.rotate(gray, cv2.ROTATE_180), 160)
    cover_pdf(tmp_path / "master.pdf", "COVER|alg2-u3-l1-gs|MASTER")
    (gray,) = scan.render_pages(tmp_path / "master.pdf", 300)
    with pytest.raises(scan.CoverPage):
        scan.decode_codes(gray, 160)


def test_no_file_in_the_data_folder_holds_the_name(run):
    data, _ = run
    files = [p for p in data.rglob("*") if p.is_file()]
    assert any(p.name == "grading.sqlite" for p in files)
    for p in files:
        raw = p.read_bytes().lower()
        for w in WORDS:
            for enc in ("utf-8", "utf-16-le"):
                assert w.encode(enc) not in raw, f"{p.relative_to(data)} holds {w!r}"


def test_no_database_text_holds_the_name_or_the_cover_code(run):
    data, _ = run
    conn = sqlite3.connect(data / "grading.sqlite")
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for t in tables:
            for row in conn.execute(f"SELECT * FROM {t}"):
                for v in row:
                    if isinstance(v, str):
                        assert not any(w in v.lower() for w in WORDS), f"{t}: {v[:80]}"
        (skipped,) = conn.execute("SELECT skipped_pages FROM scans").fetchone()
        assert json.loads(skipped) == [{"scan_page": COVER_AT, "reason": "cover"}]
        assert conn.execute("SELECT page_count FROM scans").fetchone()[0] == 13
        assert conn.execute("SELECT count(*) FROM pages WHERE scan_page = ?", (COVER_AT,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_no_image_is_stored_for_the_cover(run):
    data, _ = run
    (folder,) = (data / "pages").iterdir()
    names = sorted(p.name for p in folder.iterdir())
    assert f"{COVER_AT:03d}.png" not in names and len(names) == 12


def test_every_other_page_still_places(run):
    data, _ = run
    truth = read_csv(SYNTHETIC / "scan-pages.csv")
    conn = sqlite3.connect(data / "grading.sqlite")
    try:
        rows = conn.execute("SELECT scan_page, status, tracking_id, page FROM pages ORDER BY scan_page").fetchall()
        assert conn.execute("SELECT count(*) FROM crops").fetchone()[0] == 12 * 8
    finally:
        conn.close()
    assert len(rows) == len(truth) == 12
    for scan_page, status, tid, page in rows:
        orig = scan_page if scan_page < COVER_AT else scan_page - 1
        assert status == "placed"
        assert (tid, page) == (truth[orig - 1]["tracking_id"], int(truth[orig - 1]["page"]))


def test_ingest_summary_and_status_count_the_cover(run):
    _, out = run
    assert "12 placed, 0 duplicate, 0 unplaced, 1 cover page skipped" in out["ingest"]
    assert f"page {COVER_AT}: skipped, cover" in out["ingest"]
    assert "pull covers from the stack" in out["ingest"]
    assert "covers     1 skipped" in out["status"]
    assert NAME not in out["ingest"] + out["status"] and "COVER|" not in out["ingest"] + out["status"]
