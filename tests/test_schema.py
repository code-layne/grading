import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_name_columns(conn):
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    assert {"scans", "pages", "crops", "assignments", "questions", "submissions",
            "readings", "rulings", "scores"} <= set(tables)
    forbidden = ("name", "legal", "preferred", "email", "student")
    for t in tables:
        for col in conn.execute(f"PRAGMA table_info({t})"):
            name = col["name"]
            assert name == "filename" or not any(w in name for w in forbidden), f"{t}.{name}"


def test_student_records_keyed_by_tracking_id(conn):
    for t in ("pages", "crops", "submissions", "scores"):
        cols = {c["name"] for c in conn.execute(f"PRAGMA table_info({t})")}
        assert "tracking_id" in cols


@pytest.mark.skipif(not shutil.which("git") or not (ROOT / ".git").exists(), reason="needs the git checkout")
def test_gitignore_keeps_scans_crops_databases_and_csvs_out():
    def ignored(path):
        return subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", "--no-index", path]).returncode == 0
    for p in ["scan.pdf", "inbox/stack.pdf", "data/crops/a/K7QM2P/q1.png", "crop.jpg", "grading.sqlite",
              "grading.db", "grading.sqlite-journal", "scores.csv", "roster-map.csv", "data/anything",
              "tests/fixtures/real/roster-map.csv"]:
        assert ignored(p), p
    for p in ["tests/fixtures/real/grading_scan.pdf", "tests/fixtures/synthetic/expected-scores.csv",
              "src/grading/scan.py", "config.example.toml"]:
        assert not ignored(p), p


def test_version_1_database_is_migrated_in_place(tmp_path):
    import sqlite3

    from grading import db
    path = tmp_path / "grading.sqlite"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    old.execute("INSERT INTO meta VALUES ('schema_version', '1')")
    old.execute("INSERT INTO rulings VALUES ('a', 'q1', '3/2', 1, NULL, '2026-10-20')")
    old.commit()
    old.close()

    conn = db.connect(path)
    assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
    assert {"judgments", "api_calls", "batches", "review_queue", "review_groups"} <= tables
    assert {"request_key", "normalized"} <= {c["name"] for c in conn.execute("PRAGMA table_info(readings)")}
    assert {"crop_id", "status", "detail"} <= {c["name"] for c in conn.execute("PRAGMA table_info(scores)")}
    assert conn.execute("SELECT count(*) FROM rulings").fetchone()[0] == 1
    conn.close()
    db.connect(path).close()  # connecting again changes nothing


def test_newer_database_is_refused(tmp_path):
    from grading import db
    conn = db.connect(tmp_path / "g.sqlite")
    conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        db.connect(tmp_path / "g.sqlite")


def test_version_3_batches_survive_the_migration_and_can_be_canceled(tmp_path):
    import sqlite3

    from grading import db
    path = tmp_path / "grading.sqlite"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    for n in (2, 3):
        for stmt in db._statements(db.MIGRATIONS[n]):
            old.execute(stmt)
    old.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    old.execute("INSERT INTO batches VALUES ('msgbatch_01', '2026-10-22T08:00:00', '[]', 'in_progress', NULL)")
    old.commit()
    old.close()

    conn = db.connect(path)
    row = conn.execute("SELECT * FROM batches").fetchone()
    assert (row["id"], row["status"], row["canceled_at"]) == ("msgbatch_01", "in_progress", None)
    conn.execute("UPDATE batches SET status = 'canceling', canceled_at = '2026-10-22T10:04:00'")
    conn.execute("UPDATE batches SET status = 'canceled'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE batches SET status = 'lost'")
    conn.close()
