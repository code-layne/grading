import csv
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
