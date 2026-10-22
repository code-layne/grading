from grading.cli import main

from conftest import REAL


def _config(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(f'[paths]\ncurricula = ["{REAL / "curriculum"}"]\ninbox = "inbox"\ndata = "data"\n'
                 'roster = "roster-map.csv"\n')
    return str(f)


def test_status_before_any_ingest_creates_nothing(tmp_path, capsys):
    assert main(["--config", _config(tmp_path), "status"]) == 0
    assert "nothing ingested yet" in capsys.readouterr().out
    assert not (tmp_path / "data").exists()


def test_ingest_then_status(tmp_path, capsys):
    cfg = _config(tmp_path)
    (tmp_path / "inbox").mkdir()
    (tmp_path / "inbox" / "stack.pdf").write_bytes((REAL / "grading_scan.pdf").read_bytes())
    assert main(["--config", cfg, "ingest", "--received", "2026-10-20"]) == 0
    assert "3 placed" in capsys.readouterr().out
    assert main(["--config", cfg, "status"]) == 0
    out = capsys.readouterr().out
    assert "placed     3" in out and "3 crops to confirm" in out
