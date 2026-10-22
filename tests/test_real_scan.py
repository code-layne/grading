"""A real scan: math-shared's examples/exit_ticket, personalized for its three
invented students, printed and scanned on a Brother inkjet. Three pages, all
page 2, in reverse roster order, 300 dpi color JPEG, 2528 px wide (the
scanner trims about 0.03 in from each side). Page 1 shows through faintly,
mirrored: a ghost QR code in the top-left corner and ghost answer boxes."""

import cv2
import numpy as np
import pytest
import zxingcpp

from grading import scan
from grading.ingest import ingest_file

from conftest import REAL, border_offsets, margin_px

PDF = REAL / "grading_scan.pdf"
ROSTER_ORDER = ["K7QM2P", "T4WX9C", "H3NR8V"]  # math-shared examples/roster-map.csv


@pytest.fixture(scope="module")
def pages():
    return list(scan.render_pages(PDF, 300))


def ghost_box(layout):
    """Where page 1's QR code shows through: mirrored left to right."""
    x, y, w, h = layout["qr"]
    return [layout["paper"][0] - x - w, y, w, h]


def test_ingest_places_every_page(cfg, conn):
    result = ingest_file(cfg, PDF, conn)
    assert [p.status for p in result.pages] == ["placed"] * 3
    assert [p.tracking_id for p in result.pages] == ROSTER_ORDER[::-1]
    assert {(p.assignment, p.page) for p in result.pages} == {("sample-u0-l1-et", 2)}
    for r in conn.execute("SELECT mark_residual, rejected_codes FROM pages"):
        assert r["mark_residual"] < 3.0
        assert r["rejected_codes"] is None


def test_crops_land_on_their_boxes_and_read_blank(cfg, conn):
    ingest_file(cfg, PDF, conn)
    rows = conn.execute("SELECT * FROM crops ORDER BY page_id").fetchall()
    assert [(r["tracking_id"], r["question"]) for r in rows] == [(t, "et6") for t in ROSTER_ORDER[::-1]]
    for r in rows:
        crop = cv2.imread(str(cfg.data / r["path"]), cv2.IMREAD_GRAYSCALE)
        offsets = border_offsets(crop, margin_px(cfg.ingest), cfg.ingest.ink_threshold)
        assert None not in offsets and max(map(abs, offsets)) <= 2, offsets
        # Nobody wrote in these boxes. Show-through of page 1's text and boxes
        # lies inside them, and must not count as ink.
        assert r["blank"] == 1 and r["ink_px"] == 0


def test_show_through_is_below_the_ink_threshold(pages, cfg, manifests):
    """Inside the rubric box there is show-through but no writing: before
    thresholding it holds faint marks, after it holds none at all."""
    layout = manifests.get("sample-u0-l1-et").data
    (q,) = [q for q in layout["questions"] if q["id"] == "et6"]
    for gray in pages:
        placed = scan.place_page(gray, manifests, cfg.ingest)
        x0, y0, x1, y1 = scan.box_px(q["box"], layout, 300, -(scan.BOX_RULE_PT + 3))
        inside = placed.canonical[y0:y1, x0:x1]
        assert (inside < 250).sum() > 100  # the ghost is there to be fooled by
        assert scan.ink_pixels(inside, cfg.ingest.ink_threshold, cfg.ingest.speck_px) == 0
        # The crop the model gets: nothing left but white and real ink.
        cleaned = scan.clean(inside, cfg.ingest.ink_threshold)
        assert set(cleaned[cleaned < 255].tolist()) <= set(range(cfg.ingest.ink_threshold))


def test_ghost_qr_is_never_decoded(pages, cfg, manifests):
    layout = manifests.get("sample-u0-l1-et").data
    for gray, tid in zip(pages, ROSTER_ORDER[::-1]):
        # Not from the thresholded page ingest reads...
        codes = scan.decode_codes(gray, cfg.ingest.ink_threshold)
        assert [c.text for c in codes] == [f"sample-u0-l1-et|2|{tid}"]
        # ...and not even from the raw scan.
        raw = zxingcpp.read_barcodes(gray, formats=zxingcpp.BarcodeFormat.QRCode)
        assert [b.text for b in raw] == [f"sample-u0-l1-et|2|{tid}"]
        # The ghost region of the aligned page holds no ink once thresholded.
        placed = scan.place_page(gray, manifests, cfg.ingest)
        x0, y0, x1, y1 = scan.box_px(ghost_box(layout), layout, 300, 4)
        region = placed.canonical[y0:y1, x0:x1]
        assert region.min() < 250  # the ghost is really there
        assert scan.ink_pixels(region, cfg.ingest.ink_threshold, 1) == 0


def _paste_code(gray, src_gray, src_code, at, level):
    """Copy the QR code around src_code into gray, centered on `at`, with
    its ink drawn at gray level `level` (a stand-in for show-through)."""
    half = int(16 * 300 / 72) + 8
    sx, sy = map(int, src_code.center)
    patch = src_gray[sy - half:sy + half, sx - half:sx + half]
    ax, ay = map(int, at)
    out = gray.copy()
    region = out[ay - half:ay + half, ax - half:ax + half]
    region[patch < 128] = level
    return out


@pytest.mark.parametrize("level", [210, 60])
def test_ghost_position_never_wins(pages, cfg, manifests, level):
    """A readable code at the ghost position, faint (show-through, 210) or
    dark (60, worse than any show-through), is never taken as the page's.
    Faint: thresholding removes it. Dark: it decodes but is rejected, because
    it isn't where the manifest puts the page's code."""
    layout = manifests.get("sample-u0-l1-et").data
    gray, other = pages[0], pages[2]
    (other_code,) = scan.decode_codes(other, cfg.ingest.ink_threshold)
    placed = scan.place_page(gray, manifests, cfg.ingest)
    x, y, w, h = ghost_box(layout)
    s = 300 / 72
    at = scan.apply(placed.transform, (x + w / 2) * s, (layout["paper"][1] - y - h / 2) * s)
    fooled = _paste_code(gray, other, other_code, at, level)
    decoded = {c.text for c in scan.decode_codes(fooled, cfg.ingest.ink_threshold)}
    if level >= cfg.ingest.ink_threshold:
        assert other_code.text not in decoded
    else:
        assert other_code.text in decoded
    again = scan.place_page(fooled, manifests, cfg.ingest)
    assert again.code.tracking_id == ROSTER_ORDER[2]
    assert [c.text for c in again.rejected_codes] == ([other_code.text] if level < 160 else [])


def test_upside_down_page_is_placed(pages, cfg, manifests):
    placed = scan.place_page(np.ascontiguousarray(pages[1][::-1, ::-1]), manifests, cfg.ingest)
    assert placed.code.tracking_id == ROSTER_ORDER[1]


def test_border_search_corrects_uneven_paper_feed(pages, cfg, manifests):
    """On page 1 the paper fed unevenly: with the four marks fitted to about a
    pixel, the rubric box still sits 7px (1.7pt) lower than the affine
    transform puts it. The border search finds the printed box."""
    layout = manifests.get("sample-u0-l1-et").data
    (q,) = [q for q in layout["questions"] if q["id"] == "et6"]
    placed = scan.place_page(pages[0], manifests, cfg.ingest)
    estimate = scan.box_px(q["box"], layout, 300)
    found = scan.find_border(placed.canonical, q["box"], layout, 300, cfg.ingest.ink_threshold)
    assert found[1] - estimate[1] >= 4  # top edge: the transform alone is off
    crop, _, _ = scan.crop_box(placed.canonical, q["box"], layout, cfg.ingest)
    offsets = border_offsets(crop, margin_px(cfg.ingest), cfg.ingest.ink_threshold)
    assert max(map(abs, offsets)) <= 1


def test_border_not_found_falls_back_to_the_transform(pages, cfg, manifests):
    layout = manifests.get("sample-u0-l1-et").data
    (q,) = [q for q in layout["questions"] if q["id"] == "et6"]
    placed = scan.place_page(pages[2], manifests, cfg.ingest)
    page = placed.canonical.copy()
    x0, y0, x1, y1 = scan.box_px(q["box"], layout, 300, 6)
    page[y0:y1, x0:x1] = 255  # no border anywhere near the box
    assert scan.find_border(page, q["box"], layout, 300, cfg.ingest.ink_threshold) == scan.box_px(q["box"], layout, 300)
