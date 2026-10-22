"""The review page, through Flask's test client: it renders every group and
the unplaced queue, takes decisions only with its token and only on
localhost, and never shows a name."""

import dataclasses
import shutil

import pytest

from grading import roster
from grading.webapp import create_app

from conftest import FAKE_ROSTER, roster_names

TOKEN = "test-token"
HOST = {"base_url": "http://127.0.0.1:5005"}


@pytest.fixture
def client(graded, tmp_path, monkeypatch):
    cfg, conn = graded
    # A real roster where the config says, so a stray read would find names...
    shutil.copy(FAKE_ROSTER, tmp_path / "roster-map.csv")
    cfg = dataclasses.replace(cfg, roster=tmp_path / "roster-map.csv")

    # ...and no way to read it while review runs.
    def refuse(*a, **k):
        raise AssertionError("review read the roster")
    monkeypatch.setattr(roster, "load", refuse)
    app = create_app(cfg, token=TOKEN)
    return app.test_client(), cfg, conn


def post(c, url, data, token=TOKEN):
    return c.post(url, json=data, headers={"X-Grading-Token": token}, **HOST)


def every_page(c):
    """Every page the app serves for the current state, as text."""
    first = c.get("/", **HOST)
    assert first.status_code == 200
    out = [first.get_data(as_text=True)]
    n = out[0].count('href="/?i=')
    for url in [f"/?i={i}" for i in range(1, n)] + ["/unplaced"]:
        r = c.get(url, **HOST)
        assert r.status_code == 200, url
        out.append(r.get_data(as_text=True))
    return out


def test_no_names_anywhere(client):
    c, cfg, conn = client
    pages = every_page(c)
    assert len(pages) > 10
    text = "\n".join(pages)
    for name in roster_names():
        assert name not in text, name
    # Students appear by tracking id.
    assert "TYME2T" in text


def test_every_group_renders_crops_reading_score_feedback(client):
    c, cfg, conn = client
    pages = every_page(c)
    for html in pages[:-1]:
        assert "/crop/" in html and 'id="feedback"' in html or "Blank boxes to confirm" in html
    rubric = next(h for h in pages if "Evidence" in h)
    assert "Proposed score" in rubric and "Criterion" in rubric
    crop = conn.execute("SELECT id FROM crops LIMIT 1").fetchone()[0]
    r = c.get(f"/crop/{crop}.png", **HOST)
    assert r.status_code == 200 and r.data[:4] == b"\x89PNG"


def test_posts_need_the_token_and_localhost(client):
    c, cfg, conn = client
    key = ["alg2-u3-l1-gs", "gs5", "3x^3+4x^2-5x-4"]
    assert post(c, "/api/approve", {"key": key}, token="wrong").status_code == 403
    assert c.post("/api/approve", json={"key": key}, **HOST).status_code == 403
    assert c.get("/", base_url="http://evil.example:5005").status_code == 403
    assert conn.execute("SELECT count(*) FROM rulings").fetchone()[0] == 0
    r = post(c, "/api/approve", {"key": key, "points": "2", "feedback": "Good."})
    assert r.status_code == 200, r.get_json()
    assert "ruling saved" in r.get_json()["message"]
    assert conn.execute("SELECT points, feedback FROM rulings").fetchone()[:] == (2, "Good.")


def test_decisions_through_the_api(client):
    c, cfg, conn = client
    bad = post(c, "/api/approve", {"key": ["alg2-u3-l1-gs", "gs5", "3x^3+4x^2-5x-4"], "points": "9"})
    assert bad.status_code == 400 and "from 0 to 2" in bad.get_json()["error"]
    cid = conn.execute("SELECT crop_id FROM review_queue WHERE question = 'gs6' AND resolved_at IS NULL").fetchone()[0]
    assert post(c, "/api/reading", {"crop_id": cid, "reading": "3/2"}).status_code == 200
    blanks = [r[0] for r in conn.execute("SELECT crop_id FROM review_queue WHERE reasons = '[\"blank\"]'")]
    assert post(c, "/api/blanks", {"confirm": blanks}).status_code == 200
    assert conn.execute("SELECT count(*) FROM review_queue WHERE resolved_at IS NULL AND question IN ('gs6')"
                        ).fetchone()[0] == 0


def test_unplaced_page_view(client):
    c, cfg, conn = client
    html = c.get("/unplaced", **HOST).get_data(as_text=True)
    assert "No unplaced pages" in html
    r = c.get("/api/candidates?assignment=alg2-u3-l1-gs&page=1", **HOST).get_json()
    assert r["missing_this_page"] == [] and len(r["known"]) == 12


def test_placing_an_unplaced_page_through_the_api(client, tmp_path):
    import pymupdf
    from grading.ingest import ingest_file
    from conftest import SYNTHETIC, gray_to_pdf_page, page_image
    from test_synthetic import damage_qr
    c, cfg, conn = client
    damaged, _ = damage_qr(page_image(SYNTHETIC / "class-set-scan.pdf", 1), cfg.ingest.ink_threshold)
    pdf = tmp_path / "Arden late.pdf"  # a file name with a name in it
    with pymupdf.open() as doc:
        gray_to_pdf_page(doc, damaged)
        doc.save(pdf)
    (p,) = ingest_file(cfg, pdf, conn).pages
    html = c.get("/unplaced", **HOST).get_data(as_text=True)
    assert f"Page #{p.page_id}" in html and "qr-unreadable" in html and "Arden" not in html
    assert c.get(f"/page/{p.page_id}.png", **HOST).status_code == 200
    r = post(c, "/api/place", {"page_id": p.page_id, "assignment": "alg2-u3-l1-gs", "page": 1,
                               "tracking_id": "ZZZZZ0"})
    assert r.status_code == 400 and "not a tracking id" in r.get_json()["error"]
    r = post(c, "/api/place", {"page_id": p.page_id, "assignment": "alg2-u3-l1-gs", "page": 1,
                               "tracking_id": "462HAN"})
    assert r.status_code == 200 and "duplicate" in r.get_json()["message"]
