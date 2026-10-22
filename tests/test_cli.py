from pathlib import Path

from grading.cli import main

from conftest import REAL, SYNTHETIC


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


def _synthetic_config(tmp_path, extra=""):
    f = tmp_path / "config.toml"
    f.write_text(f'[paths]\ncurricula = ["{SYNTHETIC / "curriculum"}"]\ninbox = "inbox"\ndata = "data"\n'
                 f'roster = "roster-map.csv"\n[api]\nbatch_poll_seconds = 0\n{extra}')
    return str(f)


def test_eval_with_the_fake_client(tmp_path, capsys):
    cfg = _synthetic_config(tmp_path)
    assert main(["--config", cfg, "eval", "--fake", "--workdir", str(tmp_path / "eval"),
                 "--record", str(tmp_path / "rec")]) == 0
    out = capsys.readouterr().out
    assert "choice     48     48" in out and "rubric     12     11        1" in out
    assert "TYME2T gs8 (rubric): 2 for 1" in out and "Review: 16 items" in out
    assert "Tokens and dollars by call type" in out and (tmp_path / "eval" / "eval-report.json").is_file()

    # The recording replays with no network.
    assert main(["--config", cfg, "eval", "--replay", str(tmp_path / "rec"),
                 "--workdir", str(tmp_path / "eval2")]) == 0
    again = capsys.readouterr().out
    def table(text):  # agreement, disagreements, review and dollars; not batch ids or paths
        lines = text.splitlines()
        lines = lines[lines.index("Agreement with expected-scores.csv:"):]
        return [line for line in lines if "/eval" not in line and not line.startswith(("report:", "recording:"))]
    assert table(again) == table(out)


def test_read_record_refuses_a_folder_inside_a_repository(tmp_path, capsys):
    cfg = _synthetic_config(tmp_path)
    repo_folder = Path(__file__).resolve().parents[1] / "recording"
    assert main(["--config", cfg, "read", "--record", str(repo_folder)]) == 1
    assert "git work tree" in capsys.readouterr().err
    assert not repo_folder.exists()


def test_read_replays_a_recording_into_the_database(tmp_path, capsys):
    cfg = _synthetic_config(tmp_path)
    assert main(["--config", cfg, "eval", "--fake", "--workdir", str(tmp_path / "eval"),
                 "--record", str(tmp_path / "rec")]) == 0
    assert main(["--config", cfg, "ingest", str(SYNTHETIC / "class-set-scan.pdf")]) == 0
    capsys.readouterr()
    assert main(["--config", cfg, "read", "--replay", str(tmp_path / "rec")]) == 0
    out = capsys.readouterr().out
    assert "read 95, rubric 11, verify 94" in out and "80 final, 16 to review" in out
    assert main(["--config", cfg, "status"]) == 0
    assert "80 final, 16 to review" in capsys.readouterr().out


def test_live_read_without_a_key_says_how_to_add_one(tmp_path, capsys, monkeypatch):
    from grading import apiclient
    monkeypatch.setattr(apiclient.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["--config", _synthetic_config(tmp_path), "read"]) == 1
    assert "security add-generic-password" in capsys.readouterr().err


def test_eval_resumes_in_its_workdir(tmp_path, capsys):
    cfg = _synthetic_config(tmp_path)
    work = str(tmp_path / "eval")
    assert main(["--config", cfg, "eval", "--fake", "--workdir", work]) == 0
    assert main(["--config", cfg, "eval", "--fake", "--workdir", work]) == 0
    out = capsys.readouterr().out
    assert "resumed in" in out and out.count("submitted batch") == 3  # all from the first run
