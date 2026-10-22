"""Ingest the synthetic class set (algebra_2 unit03/lesson01/graded_sample/eval)
with one extra page whose QR code is damaged."""

import json

import cv2
import pymupdf
import pytest

from grading import config, db, scan
from grading.ingest import ingest_file

from conftest import SYNTHETIC, border_offsets, gray_to_pdf_page, margin_px, page_image, read_csv

DAMAGED_AT = 7  # the damaged page goes in as scan page 7 of 13


def damage_qr(gray, threshold):
    """White out the left half of the page's QR code: two finder patterns gone."""
    (code,) = scan.decode_codes(gray, threshold)
    out = gray.copy()
    cx, cy = code.center
    half = int(16 * 300 / 72) + 6  # half the 32bp code, plus quiet zone
    out[int(cy) - half:int(cy) + half, int(cx) - half:int(cx)] = 255
    assert scan.decode_codes(out, threshold) == []
    return out, code


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("synthetic")
    cfg = config.from_dict({"paths": {
        "curricula": [str(SYNTHETIC / "curriculum")],
        "inbox": str(tmp / "inbox"), "data": str(tmp / "data"), "roster": str(tmp / "roster-map.csv")}})
    src = SYNTHETIC / "class-set-scan.pdf"
    damaged, code = damage_qr(page_image(src, 2), cfg.ingest.ink_threshold)
    pdf = tmp / "class-set-plus-damaged.pdf"
    with pymupdf.open(src) as orig, pymupdf.open() as doc:
        doc.insert_pdf(orig, to_page=DAMAGED_AT - 2)
        gray_to_pdf_page(doc, damaged)
        doc.insert_pdf(orig, from_page=DAMAGED_AT - 1)
        doc.save(pdf)
    result = ingest_file(cfg, pdf)
    conn = db.connect(cfg.database)
    yield cfg, result, conn, code
    conn.close()


def test_every_page_placed_except_the_damaged_one(run):
    cfg, result, conn, _ = run
    truth = read_csv(SYNTHETIC / "scan-pages.csv")
    assert len(result.pages) == len(truth) + 1
    placed = [p for p in result.pages if p.status == "placed"]
    assert len(placed) == len(truth) == 12
    # scan-pages.csv numbers the original scan; pages from DAMAGED_AT on moved down one.
    for p in placed:
        orig = p.scan_page if p.scan_page < DAMAGED_AT else p.scan_page - 1
        row = truth[orig - 1]
        assert (p.tracking_id, p.page, p.assignment) == (row["tracking_id"], int(row["page"]), "alg2-u3-l1-gs")


def test_damaged_qr_goes_to_unplaced_queue(run):
    cfg, result, conn, code = run
    (unplaced,) = [p for p in result.pages if p.status == "unplaced"]
    assert unplaced.scan_page == DAMAGED_AT
    assert unplaced.reason == "qr-unreadable"
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (unplaced.page_id,)).fetchone()
    assert row["tracking_id"] is None and row["assignment"] is None
    assert conn.execute("SELECT count(*) FROM crops WHERE page_id = ?", (unplaced.page_id,)).fetchone()[0] == 0
    # Its stored image is strip-blanked like every other.
    img = cv2.imread(str(cfg.data / row["image_path"]), cv2.IMREAD_GRAYSCALE)
    x0, y0, x1, y1 = scan.box_px(scan.DEFAULT_LAYOUT["strip"], scan.DEFAULT_LAYOUT, 300)
    assert (img[y0:y1, x0:x1] == 255).all()


def test_every_crop_lands_on_its_box(run):
    cfg, result, conn, _ = run
    m = margin_px(cfg.ingest)
    rows = conn.execute("SELECT c.*, q.type FROM crops c JOIN questions q USING (assignment, question)").fetchall()
    assert len(rows) == 12 * 8
    worst = 0
    for r in rows:
        crop = cv2.imread(str(cfg.data / r["path"]), cv2.IMREAD_GRAYSCALE)
        offsets = border_offsets(crop, m, cfg.ingest.ink_threshold)
        assert None not in offsets, f"{r['tracking_id']} {r['question']}: border not found {offsets}"
        worst = max(worst, *map(abs, offsets))
    assert worst <= 2, f"a crop is off its box by {worst}px"  # 2px = 0.5pt at 300 dpi


def test_blank_boxes_match_the_answer_key(run):
    cfg, result, conn, _ = run
    expected = {(r["tracking_id"], r["item"]) for r in read_csv(SYNTHETIC / "expected-scores.csv")
                if r["category"] == "blank"}
    found = {(r["tracking_id"], r["question"])
             for r in conn.execute("SELECT tracking_id, question FROM crops WHERE blank = 1")}
    assert found == expected and len(expected) == 1


def test_stored_pages_have_the_strip_blanked(run):
    cfg, result, conn, _ = run
    for r in conn.execute("SELECT p.image_path, a.manifest_json FROM pages p JOIN assignments a USING (assignment)"):
        layout = json.loads(r["manifest_json"])
        img = cv2.imread(str(cfg.data / r["image_path"]), cv2.IMREAD_GRAYSCALE)
        x0, y0, x1, y1 = scan.box_px(layout["strip"], layout, 300, scan.STRIP_BLEED_PT)
        assert (img[y0:y1, x0:x1] == 255).all()


def test_submissions_recorded_by_tracking_id(run):
    cfg, result, conn, _ = run
    rows = conn.execute("SELECT * FROM submissions").fetchall()
    assert len(rows) == 12
    assert all(r["first_received_on"] == r["last_received_on"] for r in rows)
