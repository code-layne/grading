"""Before a live run: the number of calls and an estimated cost, a
confirmation, and a ceiling that refuses outright."""

import pytest

from grading.fake import FakeTransport
from grading.pipeline import Cancelled, CostRefused, Pipeline

from conftest import EXPECTED


def pipeline(cfg, conn, transport, confirm):
    return Pipeline(cfg, conn, transport, confirm=confirm, out=lambda *a: None, sleep=lambda s: None)


def test_declining_sends_nothing(class_set):
    cfg, conn = class_set
    asked = []
    live = FakeTransport(EXPECTED, live=True)
    with pytest.raises(Cancelled):
        pipeline(cfg, conn, live, lambda prompt: asked.append(prompt) or False).run()
    # Upper bound before reading: 95 reads, 12 rubric calls (groups unknown), 95 verifies.
    (prompt,) = asked
    assert prompt.startswith("Send 202 calls, about $")
    assert live.store["requests"] == [] and live.store["submissions"] == 0
    assert conn.execute("SELECT count(*) FROM api_calls").fetchone()[0] == 0


def test_confirming_runs(class_set):
    cfg, conn = class_set
    asked = []
    live = FakeTransport(EXPECTED, live=True)
    s = pipeline(cfg, conn, live, lambda prompt: asked.append(prompt) or True).run()
    assert len(asked) == 1 and sum(s.calls.values()) == 200
    assert s.estimate.mode == "batch" and s.estimate.calls == {"read": 95, "rubric": 12, "verify": 95}


def test_ceiling_refuses_without_asking(class_set):
    cfg, conn = class_set
    cfg.api.max_run_usd = 0.05
    asked = []
    live = FakeTransport(EXPECTED, live=True)
    with pytest.raises(CostRefused, match="max_run_usd"):
        pipeline(cfg, conn, live, lambda prompt: asked.append(prompt) or True).run()
    assert asked == [] and live.store["requests"] == []


def test_estimate_prices_batch_at_half(class_set):
    cfg, conn = class_set
    p = pipeline(cfg, conn, FakeTransport(EXPECTED), None)
    s = p._state()
    items = p.items()
    batch, standard = p.estimate(items, s, "batch"), p.estimate(items, s, "standard")
    assert batch.total_usd == pytest.approx(standard.total_usd / 2, rel=0.01)
    # Opus 5.5 at $4 / $20 per million: input plus output for every call.
    n_in, n_out = sum(standard.input_tokens.values()), sum(standard.output_tokens.values())
    assert standard.total_usd == pytest.approx((n_in * 4 + n_out * 20) / 1e6)
    # The gs8 crop is about 2075 x 440 px: 75 x 16 patches of 28 px.
    assert min(standard.input_tokens["rubric"] / 12, 10_000) > 1200


def test_offline_transports_are_not_asked(class_set):
    cfg, conn = class_set
    s = pipeline(cfg, conn, FakeTransport(EXPECTED), None).run()
    assert sum(s.calls.values()) == 200
