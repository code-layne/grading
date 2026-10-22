import csv
import datetime as dt
from pathlib import Path

import cv2
import pytest

from grading import config, db, scan
from grading.manifests import ManifestIndex

FIXTURES = Path(__file__).parent / "fixtures"
REAL = FIXTURES / "real"
SYNTHETIC = FIXTURES / "synthetic"


@pytest.fixture
def cfg(tmp_path):
    return config.from_dict({"paths": {
        "curricula": [str(REAL / "curriculum"), str(SYNTHETIC / "curriculum")],
        "inbox": str(tmp_path / "inbox"),
        "data": str(tmp_path / "data"),
        "roster": str(tmp_path / "rosters" / "roster-map.csv"),
    }})


@pytest.fixture
def conn(cfg):
    c = db.connect(cfg.database)
    yield c
    c.close()


@pytest.fixture(scope="session")
def manifests():
    return ManifestIndex([REAL / "curriculum", SYNTHETIC / "curriculum"])


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def border_offsets(crop, margin_px, threshold, search=8):
    """Where the box border sits in a crop, relative to where it should be
    (margin_px in from each edge): (top, bottom, left, right) in pixels, or
    None for a side whose border isn't found within `search` pixels.

    A border row is one where most of the box's width is ink."""
    ink = crop < threshold
    h, w = ink.shape
    inner_cols = slice(margin_px + 10, w - margin_px - 10)
    inner_rows = slice(margin_px + 10, h - margin_px - 10)
    out = []
    for side, expected in (("top", margin_px), ("bottom", h - 1 - margin_px),
                           ("left", margin_px), ("right", w - 1 - margin_px)):
        best = None
        for d in range(-search, search + 1):
            i = expected + d
            if not 0 <= i < (h if side in ("top", "bottom") else w):
                continue
            line = ink[i, inner_cols] if side in ("top", "bottom") else ink[inner_rows, i]
            if line.mean() >= 0.7 and (best is None or abs(d) < abs(best)):
                best = d
        out.append(best)
    return tuple(out)


def margin_px(settings):
    return int(round(settings.crop_margin_pt * settings.dpi / 72.0))


def page_image(pdf, index, dpi=300):
    for i, g in enumerate(scan.render_pages(pdf, dpi)):
        if i == index:
            return g
    raise IndexError(index)


def gray_to_pdf_page(doc, gray):
    """Append a grayscale image to a PyMuPDF document as a letter page."""
    ok, png = cv2.imencode(".png", gray)
    assert ok
    page = doc.new_page(width=612, height=792)
    page.insert_image(page.rect, stream=png.tobytes())


EXPECTED = SYNTHETIC / "expected-scores.csv"


def synthetic_cfg(root):
    cfg = config.from_dict({"paths": {
        "curricula": [str(SYNTHETIC / "curriculum")], "inbox": str(root / "inbox"),
        "data": str(root / "data"), "roster": str(root / "roster-map.csv")}})
    cfg.api.batch_poll_seconds = 0
    return cfg


@pytest.fixture(scope="session")
def ingested(tmp_path_factory):
    """The synthetic class set, ingested once; class_set copies it per test."""
    from grading.ingest import ingest_file
    root = tmp_path_factory.mktemp("ingested")
    result = ingest_file(synthetic_cfg(root), SYNTHETIC / "class-set-scan.pdf")
    assert result.count("placed") == 12
    return root


@pytest.fixture
def class_set(ingested, tmp_path):
    import shutil
    shutil.copytree(ingested / "data", tmp_path / "data")
    cfg = synthetic_cfg(tmp_path)
    c = db.connect(cfg.database)
    yield cfg, c
    c.close()


FAKE_ROSTER = SYNTHETIC / "fake-roster.csv"


def fake_run(cfg, conn, transport=None):
    """Read, score and verify against the fake client."""
    from grading.fake import FakeTransport
    from grading.pipeline import Pipeline
    transport = transport or FakeTransport(EXPECTED)
    summary = Pipeline(cfg, conn, transport, out=lambda *a: None, sleep=lambda s: None).run()
    return summary, transport


def review_all(review, fixes=None):
    """Work through the whole queue the way a teacher would: confirm the
    blanks, transcribe what has no reading (fixes: {tracking id: reading}),
    and approve every proposed score."""
    fixes = fixes or {}
    while True:
        groups = review.groups()
        if not groups:
            return
        key = groups[0]["key"]
        g = review.group(key)
        if key == ["blanks"]:
            review.confirm_blanks([i["crop_id"] for i in g["items"]])
        elif g["individual"]:
            it = g["items"][0]
            review.correct(it["crop_id"], fixes[it["tracking_id"]])
        else:
            review.approve(key)


@pytest.fixture
def graded(class_set):
    """The synthetic class set, read, scored and verified by the fake client."""
    cfg, conn = class_set
    fake_run(cfg, conn)
    return cfg, conn


def roster_names():
    """Every name in the fake roster, in the forms a report could write."""
    names = set()
    for r in read_csv(FAKE_ROSTER):
        last, first = (p.strip() for p in r["legal_name"].split(",", 1))
        names |= {r["legal_name"], last, first, r["preferred_name"]}
    return names


LATE = "462HAN"     # scan page 2 of the class set; shares its gs8 answer with BKTRTN


@pytest.fixture
def late_paper(tmp_path):
    """The class set without 462HAN's page, graded and reviewed; then
    462HAN's page arrives late with a damaged QR code."""
    import pymupdf
    from grading.fake import DEFAULT_FAULTS
    from grading.ingest import ingest_file
    from grading.review import Review
    from test_synthetic import damage_qr
    cfg = synthetic_cfg(tmp_path)
    src = SYNTHETIC / "class-set-scan.pdf"
    first = tmp_path / "class-set.pdf"
    with pymupdf.open(src) as orig, pymupdf.open() as doc:
        doc.insert_pdf(orig, from_page=0, to_page=0)
        doc.insert_pdf(orig, from_page=2)
        doc.save(first)
    late = tmp_path / "late.pdf"
    damaged, _ = damage_qr(page_image(src, 1), cfg.ingest.ink_threshold)
    with pymupdf.open() as doc:
        gray_to_pdf_page(doc, damaged)
        doc.save(late)
    conn = db.connect(cfg.database)
    assert ingest_file(cfg, first, conn, received_on=dt.date(2026, 10, 20)).count("placed") == 11
    fake_run(cfg, conn)
    review_all(Review(cfg, conn), {DEFAULT_FAULTS["illegible"][0]: "2"})
    res = ingest_file(cfg, late, conn, received_on=dt.date(2026, 10, 23))
    (page,) = res.pages
    assert page.status == "unplaced" and page.reason == "qr-unreadable"
    yield cfg, conn, page.page_id
    conn.close()
