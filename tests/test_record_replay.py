"""grading read --record DIR / --replay DIR: a run saved request by request,
and the whole pipeline run again from those files with no network."""

import json

import anthropic
import pytest

from grading import evaluate
from grading.apiclient import ApiError, RecordingTransport, ReplayTransport
from grading.fake import FakeTransport

from conftest import EXPECTED, SYNTHETIC, synthetic_cfg


def strip_paths(report):
    for d in report["disagreements"]:
        d.pop("crop", None)
    report.pop("data")
    return report


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("recorded")
    cfg = synthetic_cfg(tmp)
    rec = tmp / "recording"
    report = evaluate.run(cfg, RecordingTransport(FakeTransport(EXPECTED), rec), SYNTHETIC, tmp / "eval",
                          out=lambda *a: None)
    return cfg, rec, report


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("replay must not create an API client")
    monkeypatch.setattr(anthropic, "Anthropic", refuse)


def test_recording_holds_every_call_without_images(recorded):
    cfg, rec, report = recorded
    files = sorted((rec / "calls").glob("*.json"))
    assert len(files) == 200
    for f in files:
        entry = json.loads(f.read_text())
        assert entry["response"]["content"] and entry["mode"] == "batch" and entry["batch_id"]
        (image,) = [b for b in entry["request"]["messages"][0]["content"] if b["type"] == "image"]
        assert "data" not in image["source"] and len(image["source"]["sha256"]) == 64
        assert entry["meta"]["call_type"] in ("read", "rubric", "verify")


def test_replay_reproduces_the_run_with_no_network(recorded, tmp_path, no_network):
    cfg, rec, report = recorded
    replay = ReplayTransport(rec)
    again = evaluate.run(synthetic_cfg(tmp_path), replay, SYNTHETIC, tmp_path / "eval", out=lambda *a: None)
    assert replay.stale == []
    assert strip_paths(again) == strip_paths(json.loads(json.dumps(report)))
    # Recorded as batch calls, so replayed dollars carry the batch discount too.
    assert again["spent"]["read"]["usd"] == report["spent"]["read"]["usd"] > 0


def test_replay_flags_requests_that_changed_since_recording(recorded, tmp_path, no_network):
    cfg, rec, _ = recorded
    cfg2 = synthetic_cfg(tmp_path)
    cfg2.api.read_effort = "high"
    replay = ReplayTransport(rec)
    report = evaluate.run(cfg2, replay, SYNTHETIC, tmp_path / "eval", out=lambda *a: None)
    assert len(replay.stale) == 95 and all(k.startswith("read-") for k in replay.stale)
    assert "differ from the recording" in "\n".join(evaluate.lines(report))


def test_replay_without_a_response_is_an_error(recorded, tmp_path, no_network):
    cfg, rec, _ = recorded
    partial = tmp_path / "partial"
    (partial / "calls").mkdir(parents=True)
    for f in list((rec / "calls").glob("*.json"))[:20]:
        (partial / "calls" / f.name).write_text(f.read_text())
    with pytest.raises(ApiError, match="no response for"):
        evaluate.run(synthetic_cfg(tmp_path), ReplayTransport(partial), SYNTHETIC, tmp_path / "eval",
                     out=lambda *a: None)
