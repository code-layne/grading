"""A stand-in for the API, answering from expected-scores.csv.

Until a real recording exists, this is what the tests run the whole
pipeline against: every read returns the response the eval set says the
student wrote, every rubric call the expected criterion scores, and every
verify agrees, except for four injected faults that the pipeline must catch:

  illegible            the read pass can't read one crop
  low_confidence       one reading is right but comes back with low confidence
  verify_disagreement  the verify pass reads one choice item as another letter
  rubric_off_by_one    the rubric call gives one item a point too many

Responses are shaped like the API's (a tool_use block, usage with cache
writes on a prefix's first use and cache reads after), so the pipeline's
parsing and the eval's token report are exercised too. It works out which
crop a request is for from the request's metadata, which a real call never
sees.
"""

import base64
import csv
import hashlib
import json
from pathlib import Path

from . import costs, prompts
from .apiclient import Outcome, Transport

DEFAULT_FAULTS = {
    "illegible": ("GV2GN5", "gs6"),
    "low_confidence": ("RAU7NS", "gs5"),
    "verify_disagreement": ("2US2Q7", "gs1"),
    "rubric_off_by_one": ("TYME2T", "gs8"),
}
OUTPUT_TOKENS = {"read": 180, "rubric": 900, "verify": 420}


def load_expected(path: Path) -> dict:
    with open(path, newline="") as f:
        return {(r["tracking_id"], r["item"]): r for r in csv.DictReader(f)}


def criteria_of(row) -> dict:
    return {k: float(v) for k, v in (part.split("=") for part in row["criteria"].split(";"))}


class FakeTransport(Transport):
    def __init__(self, expected_csv: Path, faults: dict | None = None, *, live=False, store=None,
                 polls_before_end=1, overrides=None, refuse=(), fail_once=(), ran_before_cancel=0):
        """overrides: {(tracking id, question): reading} for both passes.
        ran_before_cancel: how many of a batch's requests had run when it was
        canceled; the rest come back canceled.
        refuse: {(tracking id, question, call type)} answered with a refusal.
        fail_once: the same, answered with a transient error the first time."""
        self.expected = load_expected(expected_csv)
        self.faults = DEFAULT_FAULTS if faults is None else faults
        self.overrides = overrides or {}
        self.refuse = set(refuse)
        self.fail_once = set(fail_once)
        self.live = live
        # Shared between instances to stand for the API's side of a batch
        # across separate runs of the command.
        self.store = store if store is not None else {}
        self.store.setdefault("batches", {})
        self.store.setdefault("submissions", 0)
        self.store.setdefault("prefixes", set())
        self.store.setdefault("requests", [])
        self.store.setdefault("failed_once", set())
        self.polls_before_end = polls_before_end
        self.ran_before_cancel = ran_before_cancel

    def _is(self, fault, meta):
        return self.faults.get(fault) == (meta["tracking_id"], meta["question"])

    def _input(self, call_type, meta):
        row = self.expected[(meta["tracking_id"], meta["question"])]
        text = self.overrides.get((meta["tracking_id"], meta["question"]), row["response"])
        reading = {"reading": text, "latex": text, "confidence": "high", "illegible": False}
        if call_type == "read":
            if self._is("illegible", meta):
                reading.update(reading="", latex="", illegible=True, confidence="low")
            if self._is("low_confidence", meta):
                reading["confidence"] = "low"
            return reading
        crit = None
        if meta["type"] == "rubric":
            crit = criteria_of(row)
            if call_type == "rubric" and self._is("rubric_off_by_one", meta):
                k = next((k for k, v in crit.items() if v == 0), next(iter(crit)))
                crit[k] = 1.0 - crit[k]
            crit = {k: {"points": int(v) if v.is_integer() else v,
                        "evidence": row["response"][:60] if v else "", "confidence": "high"}
                    for k, v in crit.items()}
        if call_type == "rubric":
            return {"criteria": crit, "feedback": "Which step changes every sign? Look at that line again."}
        out = dict(reading, agrees=True, dispute="")
        if crit is not None:
            out["criteria"] = crit
        if self._is("verify_disagreement", meta):
            letter = row["response"].strip()[:1] or "A"
            out.update(reading=chr((ord(letter) - 65 + 3) % 26 + 65), agrees=False,
                       dispute="The letter in the box reads differently.")
        if self._is("rubric_off_by_one", meta):
            out.update(agrees=False, dispute="One criterion is scored differently.")
        return out

    def _usage(self, params):
        prefix = json.dumps([params["model"], params["tools"], params["system"]], sort_keys=True)
        n_prefix = int(len(prefix) / costs.CHARS_PER_TOKEN) + costs.TOOL_SYSTEM_TOKENS
        png = next(base64.standard_b64decode(b["source"]["data"]) for b in params["messages"][0]["content"]
                   if b["type"] == "image")
        n_rest = costs.image_tokens(png) + 40
        ttl = params["system"][-1]["cache_control"].get("ttl")
        digest = hashlib.sha256(prefix.encode()).hexdigest()
        if digest in self.store["prefixes"]:
            return {"input_tokens": n_rest, "cache_read_input_tokens": n_prefix, "cache_creation_input_tokens": 0,
                    "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0}}
        self.store["prefixes"].add(digest)
        return {"input_tokens": n_rest, "cache_read_input_tokens": 0, "cache_creation_input_tokens": n_prefix,
                "cache_creation": {"ephemeral_5m_input_tokens": 0 if ttl == "1h" else n_prefix,
                                   "ephemeral_1h_input_tokens": n_prefix if ttl == "1h" else 0}}

    def _outcome(self, key, params, meta, mode="standard", batch_id=None):
        which = (meta["tracking_id"], meta["question"], meta["call_type"])
        if which in self.fail_once and which not in self.store["failed_once"]:
            self.store["failed_once"].add(which)
            self.store["requests"].append(key)
            return Outcome(key, None, "errored overloaded_error", mode=mode, batch_id=batch_id)
        return Outcome(key, self._message(key, params, meta), mode=mode, batch_id=batch_id)

    def _message(self, key, params, meta):
        call_type = meta["call_type"]
        self.store["requests"].append(key)
        usage = self._usage(params)
        if (meta["tracking_id"], meta["question"], call_type) in self.refuse:
            usage["output_tokens"] = 0
            return {"id": f"msg_fake_{key}", "type": "message", "role": "assistant", "model": params["model"],
                    "content": [], "stop_reason": "refusal", "stop_sequence": None, "usage": usage,
                    "stop_details": {"type": "refusal", "category": None, "explanation": None}}
        usage["output_tokens"] = OUTPUT_TOKENS[call_type]
        return {"id": f"msg_fake_{key}", "type": "message", "role": "assistant", "model": params["model"],
                "content": [{"type": "thinking", "thinking": "", "signature": "fake"},
                            {"type": "tool_use", "id": f"toolu_fake_{key}", "name": prompts.TOOL_NAMES[call_type],
                             "input": self._input(call_type, meta)}],
                "stop_reason": "tool_use", "stop_sequence": None, "usage": usage}

    def create(self, key, params, meta):
        return self._outcome(key, params, meta)

    def batch_submit(self, items):
        self.store["submissions"] += 1
        batch_id = f"msgbatch_fake{self.store['submissions']:03d}"
        self.store["batches"][batch_id] = {"items": items, "polls": 0}
        return batch_id

    def batch_status(self, batch_id):
        b = self.store["batches"][batch_id]
        b["polls"] += 1
        n = len(b["items"])
        if "ran" in b:  # canceled: canceling for one poll, then ended
            if b["polls"] > b["canceled_at_poll"] + 1:
                return {"status": "ended", "counts": {"processing": 0, "succeeded": b["ran"],
                                                      "canceled": n - b["ran"]}}
            return {"status": "canceling", "counts": {"processing": n - b["ran"], "succeeded": b["ran"]}}
        if b["polls"] > self.polls_before_end:
            return {"status": "ended", "counts": {"processing": 0, "succeeded": n}}
        return {"status": "in_progress", "counts": {"processing": n, "succeeded": 0}}

    def batch_cancel(self, batch_id):
        b = self.store["batches"][batch_id]
        b.update(ran=min(self.ran_before_cancel, len(b["items"])), canceled_at_poll=b["polls"])
        self.store.setdefault("canceled", []).append(batch_id)
        return {"status": "canceling", "counts": {"processing": len(b["items"]) - b["ran"], "succeeded": b["ran"]}}

    def batch_results(self, batch_id):
        b = self.store["batches"][batch_id]
        for i, (key, params, meta) in enumerate(b["items"]):
            if "ran" in b and i >= b["ran"]:
                yield Outcome(key, None, "canceled", mode="batch", batch_id=batch_id)
            else:
                yield self._outcome(key, params, meta, "batch", batch_id)
