"""Tokens and dollars: what a run is estimated to cost before it starts, and
what each call cost afterwards, from the prices in config.

Image tokens follow the vision docs: ceil(w/28) * ceil(h/28), at most 4,784
on Claude 4.7 and later. Text is estimated at 3.5 characters a token, plus
the docs' 286-token tool-use system prompt for Opus and Sonnet 5.5. The
estimate assumes no cache hits, so a real run should come in under it.
"""

import json
import math
import struct
from dataclasses import dataclass, field

from .config import BATCH_DISCOUNT, ConfigError

TOOL_SYSTEM_TOKENS = 286
MAX_IMAGE_TOKENS = 4784
CHARS_PER_TOKEN = 3.5
CALL_TYPES = ("read", "rubric", "verify")


def png_size(png: bytes) -> tuple[int, int]:
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return struct.unpack(">II", png[16:24])


def image_tokens(png: bytes) -> int:
    w, h = png_size(png)
    return min(math.ceil(w / 28) * math.ceil(h / 28), MAX_IMAGE_TOKENS)


def input_tokens(params: dict, png: bytes) -> int:
    text = json.dumps(params["tools"]) + "".join(b["text"] for b in params["system"])
    for msg in params["messages"]:
        text += "".join(b.get("text", "") for b in msg["content"])
    return int(len(text) / CHARS_PER_TOKEN) + TOOL_SYSTEM_TOKENS + image_tokens(png)


def usage_of(message: dict | None) -> dict:
    u = (message or {}).get("usage") or {}
    created = u.get("cache_creation") or {}
    w5, w1 = created.get("ephemeral_5m_input_tokens"), created.get("ephemeral_1h_input_tokens")
    if w5 is None and w1 is None:
        w5, w1 = u.get("cache_creation_input_tokens") or 0, 0
    return {"input_tokens": u.get("input_tokens") or 0, "output_tokens": u.get("output_tokens") or 0,
            "cache_write_5m_tokens": w5 or 0, "cache_write_1h_tokens": w1 or 0,
            "cache_read_tokens": u.get("cache_read_input_tokens") or 0}


def dollars(prices, model: str, mode: str, usage: dict) -> float | None:
    p = prices.get(model)
    if p is None:
        return None
    total = (usage["input_tokens"] * p.input + usage["output_tokens"] * p.output
             + usage["cache_write_5m_tokens"] * p.cache_write_5m
             + usage["cache_write_1h_tokens"] * p.cache_write_1h
             + usage["cache_read_tokens"] * p.cache_read) / 1e6
    return total * (BATCH_DISCOUNT if mode == "batch" else 1.0)


@dataclass
class Estimate:
    mode: str
    calls: dict = field(default_factory=lambda: {t: 0 for t in CALL_TYPES})
    input_tokens: dict = field(default_factory=lambda: {t: 0 for t in CALL_TYPES})
    output_tokens: dict = field(default_factory=lambda: {t: 0 for t in CALL_TYPES})
    usd: dict = field(default_factory=lambda: {t: 0.0 for t in CALL_TYPES})

    def add(self, prices, call_type, model, n_in, n_out):
        p = prices.get(model)
        if p is None:
            raise ConfigError(f"no price for {model}: add [prices.\"{model}\"] to the config")
        self.calls[call_type] += 1
        self.input_tokens[call_type] += n_in
        self.output_tokens[call_type] += n_out
        self.usd[call_type] += (n_in * p.input + n_out * p.output) / 1e6 * (
            BATCH_DISCOUNT if self.mode == "batch" else 1.0)

    @property
    def total_calls(self):
        return sum(self.calls.values())

    @property
    def total_usd(self):
        return sum(self.usd.values())

    def lines(self):
        out = [f"{'call':8} {'calls':>6} {'input tok':>10} {'output tok':>11} {'est. $':>8}"]
        for t in CALL_TYPES:
            if self.calls[t]:
                out.append(f"{t:8} {self.calls[t]:>6} {self.input_tokens[t]:>10,} "
                           f"{self.output_tokens[t]:>11,} {self.usd[t]:>8.2f}")
        out.append(f"{'total':8} {self.total_calls:>6} {sum(self.input_tokens.values()):>10,} "
                   f"{sum(self.output_tokens.values()):>11,} {self.total_usd:>8.2f}")
        return out


def spent(conn, prices, after_id=0) -> dict:
    """Tokens and dollars by call type from the api_calls table (the calls
    after api_calls.id after_id). Tokens are summed per model and API before
    they are priced, so the dollars don't depend on the order of the rows
    (concurrent standard calls are stored in the order they come back)."""
    rows = conn.execute("SELECT * FROM api_calls WHERE id > ?", (after_id,)).fetchall()
    kinds = ("input_tokens", "output_tokens", "cache_write_5m_tokens", "cache_write_1h_tokens", "cache_read_tokens")
    out = {t: {"calls": 0, **{k: 0 for k in kinds}, "usd": 0.0, "unpriced": 0} for t in CALL_TYPES}
    groups = {}   # (call type, model, mode) -> summed usage
    for r in rows:
        o = out[r["call_type"]]
        o["calls"] += 1
        usage = {k: r[k] for k in kinds}
        for k, v in usage.items():
            o[k] += v
        if any(usage.values()):
            g = groups.setdefault((r["call_type"], r["model"] or "", r["mode"]), {k: 0 for k in kinds} | {"n": 0})
            g["n"] += 1
            for k, v in usage.items():
                g[k] += v
    for (call_type, model, mode), g in sorted(groups.items()):
        d = dollars(prices, model, mode, g)
        if d is None:
            out[call_type]["unpriced"] += g["n"]
        else:
            out[call_type]["usd"] += d
    return out


def spent_lines(totals: dict):
    out = [f"{'call':8} {'calls':>6} {'input':>9} {'cache wr':>9} {'cache rd':>9} {'output':>9} {'$':>8}"]
    grand = 0.0
    for t in CALL_TYPES:
        o = totals[t]
        if not o["calls"]:
            continue
        grand += o["usd"]
        note = f"  ({o['unpriced']} calls on a model with no price)" if o["unpriced"] else ""
        out.append(f"{t:8} {o['calls']:>6} {o['input_tokens']:>9,} "
                   f"{o['cache_write_5m_tokens'] + o['cache_write_1h_tokens']:>9,} "
                   f"{o['cache_read_tokens']:>9,} {o['output_tokens']:>9,} {o['usd']:>8.4f}{note}")
    out.append(f"{'total':8} {sum(totals[t]['calls'] for t in CALL_TYPES):>6} {'':>9} {'':>9} {'':>9} {'':>9} "
               f"{grand:>8.4f}")
    return out
