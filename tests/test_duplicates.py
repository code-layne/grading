import pymupdf

from grading.ingest import ingest_file, ingest_inbox

from conftest import REAL


def test_second_copy_of_a_page_is_flagged_not_replaced(cfg, conn, tmp_path):
    first = ingest_file(cfg, REAL / "grading_scan.pdf", conn)
    one = tmp_path / "late-page.pdf"
    with pymupdf.open(REAL / "grading_scan.pdf") as doc:
        doc.select([1])  # T4WX9C's page 2 again
        doc.save(one)
    again = ingest_file(cfg, one, conn)
    (dup,) = again.pages
    assert dup.status == "duplicate" and dup.reason == "duplicate-page"
    assert dup.tracking_id == "T4WX9C"
    original = first.pages[1].page_id
    row = conn.execute("SELECT * FROM pages WHERE id = ?", (dup.page_id,)).fetchone()
    assert row["duplicate_of"] == original
    # The first copy stays the placed one; the duplicate's crop is kept for review.
    assert conn.execute("SELECT status FROM pages WHERE id = ?", (original,)).fetchone()[0] == "placed"
    paths = [r[0] for r in conn.execute("SELECT path FROM crops WHERE tracking_id = 'T4WX9C' ORDER BY id")]
    assert len(paths) == 2 and paths[0] != paths[1]


def test_same_file_twice_is_skipped(cfg, conn):
    ingest_file(cfg, REAL / "grading_scan.pdf", conn)
    again = ingest_file(cfg, REAL / "grading_scan.pdf", conn)
    assert again.skipped and again.pages == []
    assert conn.execute("SELECT count(*) FROM pages").fetchone()[0] == 3


def test_inbox_is_ingested_and_moved(cfg):
    cfg.inbox.mkdir(parents=True)
    (cfg.inbox / "stack1.pdf").write_bytes((REAL / "grading_scan.pdf").read_bytes())
    (results,) = ingest_inbox(cfg)
    assert results.count("placed") == 3
    assert not (cfg.inbox / "stack1.pdf").exists()
    assert (cfg.inbox / "ingested" / "stack1.pdf").exists()
