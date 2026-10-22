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


def test_now_and_batch_are_mutually_exclusive(tmp_path, capsys):
    cfg = _synthetic_config(tmp_path)
    for cmd in (["read"], ["eval", "--fake"]):
        try:
            main(["--config", cfg, *cmd, "--now", "--batch"])
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("--now --batch was accepted")
        assert "not allowed with argument" in capsys.readouterr().err


def test_eval_now_and_batch_override_the_threshold(tmp_path, capsys):
    cfg = _synthetic_config(tmp_path)  # 95 crops: over the default threshold of 20
    assert main(["--config", cfg, "eval", "--fake", "--now", "--workdir", str(tmp_path / "now")]) == 0
    assert "submitted batch" not in capsys.readouterr().out
    small = _synthetic_config(tmp_path, "batch_threshold = 1000\n")
    assert main(["--config", small, "eval", "--fake", "--batch", "--workdir", str(tmp_path / "batch")]) == 0
    assert capsys.readouterr().out.count("submitted batch") == 3
    assert main(["--config", small, "eval", "--fake", "--workdir", str(tmp_path / "neither")]) == 0
    assert "submitted batch" not in capsys.readouterr().out


def test_first_poll_line_says_ctrl_c_is_safe_and_how_to_resume(tmp_path, capsys):
    import shlex
    cfg = _synthetic_config(tmp_path)
    work = str(tmp_path / "eval")
    assert main(["--config", cfg, "eval", "--fake", "--workdir", work]) == 0
    lines = capsys.readouterr().out.splitlines()
    i = next(i for i, line in enumerate(lines) if line.startswith("batch msgbatch_fake001: in_progress, 0 of 95"))
    assert lines[i].endswith(" done, 0m")
    resume = shlex.join(["grading", "--config", cfg, "eval", "--workdir", work, "--fake"])
    cancel = shlex.join(["grading", "--config", cfg, "batch", "cancel", "--workdir", work])
    now = shlex.join(["grading", "--config", cfg, "eval", "--workdir", work, "--now", "--fake"])
    assert lines[i + 1] == (f"  Ctrl-C is safe: the batch keeps running without this terminal, "
                            f"and `{resume}` picks it up.")
    assert lines[i + 2] == f"  To stop waiting: Ctrl-C, `{cancel}`, then `{now}`."
    assert sum("Ctrl-C is safe" in line for line in lines) == 1


def _fake_api(monkeypatch, **kw):
    """The live transport replaced by the fake client, sharing one batch
    store across commands as the API would."""
    from grading import cli
    from grading.fake import FakeTransport
    from conftest import EXPECTED
    store = {}
    monkeypatch.setattr(cli, "AnthropicTransport", lambda api: FakeTransport(EXPECTED, store=store, **kw))
    return store


def test_cancel_then_now_sends_only_what_never_ran(tmp_path, capsys, monkeypatch):
    import shlex
    store = _fake_api(monkeypatch, polls_before_end=100, ran_before_cancel=10)
    cfg = _synthetic_config(tmp_path)
    assert main(["--config", cfg, "ingest", str(SYNTHETIC / "class-set-scan.pdf")]) == 0
    assert main(["--config", cfg, "read", "--no-wait"]) == 0
    out = capsys.readouterr().out
    assert "batch msgbatch_fake001: in_progress, 0 of 95 done, 0m" in out and "Ctrl-C" not in out

    # --now refuses to send what the running batch already holds.
    read_now = shlex.join(["grading", "--config", cfg, "read", "--now"])
    cancel = shlex.join(["grading", "--config", cfg, "batch", "cancel"])
    assert main(["--config", cfg, "read", "--now"]) == 1
    err = capsys.readouterr().err
    assert "batch msgbatch_fake001 is in progress" in err and f"`{cancel}`" in err and f"`{read_now}`" in err
    assert store["submissions"] == 1 and store["requests"] == []

    assert main(["--config", cfg, "status"]) == 0
    assert "batch msgbatch_fake001 submitted 0m ago, not collected" in capsys.readouterr().out

    assert main(["--config", cfg, "batch", "cancel"]) == 0
    out = capsys.readouterr().out
    assert "batch msgbatch_fake001 canceled: 10 results stored, 85 requests never ran" in out
    assert f"`{read_now}` sends the requests that never ran" in out
    assert main(["--config", cfg, "batch", "cancel"]) == 0
    assert "no batch running" in capsys.readouterr().out

    assert main(["--config", cfg, "read", "--now"]) == 0
    out = capsys.readouterr().out
    assert "calls: read 85, rubric 11, verify 94 (standard API)" in out and "80 final, 16 to review" in out
    assert store["submissions"] == 1


def _ctrl_c_on_first_poll(monkeypatch):
    from grading.fake import FakeTransport
    real_status = FakeTransport.batch_status

    def interrupted(self, batch_id):
        if self.store["batches"][batch_id]["polls"] == 1:
            raise KeyboardInterrupt
        return real_status(self, batch_id)
    monkeypatch.setattr(FakeTransport, "batch_status", interrupted)
    return lambda: monkeypatch.setattr(FakeTransport, "batch_status", real_status)


def test_cancel_in_an_eval_workdir(tmp_path, capsys, monkeypatch):
    store = _fake_api(monkeypatch, polls_before_end=100)
    cfg = _synthetic_config(tmp_path)
    work = tmp_path / "eval"
    restore = _ctrl_c_on_first_poll(monkeypatch)
    assert main(["--config", cfg, "eval", "--workdir", str(work)]) == 130
    restore()
    capsys.readouterr()

    assert main(["--config", cfg, "batch", "cancel", "--workdir", str(work)]) == 0
    out = capsys.readouterr().out
    assert "batch msgbatch_fake001 canceled: 0 results stored, 95 requests never ran" in out
    assert f"--workdir {work} --now` sends" in out
    assert store["canceled"] == ["msgbatch_fake001"]
    assert not (tmp_path / "data").exists()  # the real data folder was never touched

    assert main(["--config", cfg, "eval", "--workdir", str(work), "--now"]) == 0
    assert "Review: 16 items" in capsys.readouterr().out and store["submissions"] == 1


def test_ctrl_c_while_waiting_leaves_the_batch_to_collect(tmp_path, capsys, monkeypatch):
    store = _fake_api(monkeypatch, polls_before_end=2)
    cfg = _synthetic_config(tmp_path)
    assert main(["--config", cfg, "ingest", str(SYNTHETIC / "class-set-scan.pdf")]) == 0
    restore = _ctrl_c_on_first_poll(monkeypatch)
    assert main(["--config", cfg, "read"]) == 130
    assert "interrupted" in capsys.readouterr().err
    restore()
    assert main(["--config", cfg, "read"]) == 0
    out = capsys.readouterr().out
    assert "resuming batch msgbatch_fake001" in out and "80 final, 16 to review" in out
    assert store["submissions"] == 3
