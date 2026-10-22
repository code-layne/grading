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
