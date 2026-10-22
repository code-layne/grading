"""Replays the live recording of the synthetic class set, once one is
committed (tests/fixtures/recordings/synthetic; see tests/fixtures/README.md).
Until then this is skipped and the fake client stands in."""

import warnings

import anthropic
import pytest

from grading import evaluate
from grading.apiclient import ReplayTransport

from conftest import FIXTURES, SYNTHETIC, synthetic_cfg

RECORDING = FIXTURES / "recordings" / "synthetic"

pytestmark = pytest.mark.skipif(not (RECORDING / "calls").is_dir(), reason="no live recording committed yet")


def test_live_recording_replays_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: pytest.fail("replay created an API client"))
    replay = ReplayTransport(RECORDING)
    report = evaluate.run(synthetic_cfg(tmp_path), replay, SYNTHETIC, tmp_path / "eval", out=lambda *a: None)
    # The responses are what they are: this checks that the code handles a
    # real run end to end, not how well the model read. Agreement is the
    # eval report's job.
    assert report["waiting"] == 0
    for t in ("choice", "short", "rubric"):
        assert report["by_type"][t]["missing"] == 0
    if replay.stale:
        warnings.warn(f"{len(replay.stale)} requests changed since the recording; "
                      "record again (tests/fixtures/README.md)")
