"""Talking to the Claude API, or to a recording of it.

A transport sends one request (`create`) or a batch of them (`batch_submit`,
`batch_status`, `batch_results`, `batch_cancel`) and hands back plain dicts shaped like the
API's own JSON, so the pipeline never cares which transport it has:

  AnthropicTransport  the live API through the Anthropic Python SDK
  RecordingTransport  wraps another transport and saves every request and response
  ReplayTransport     answers from a recording, with no network
  fake.FakeTransport  answers from expected-scores.csv (tests and `eval --fake`)

The API key comes from the macOS Keychain (`security find-generic-password
-s anthropic-grading -w`), else $ANTHROPIC_API_KEY. It is handed to the SDK
and nowhere else: never printed, logged or recorded.
"""

import base64
import copy
import datetime as dt
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ApiError(Exception):
    pass


class ApiKeyError(ApiError):
    pass


@dataclass
class Outcome:
    """What came back for one request: a message, or an error."""
    key: str
    message: dict | None
    error: str | None = None        # errored | expired | canceled, plus detail
    mode: str = "standard"          # standard | batch
    batch_id: str | None = None


def api_key(service: str) -> str:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                             capture_output=True, text=True, timeout=15)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # not a Mac, or no Keychain access: fall back to the environment
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    raise ApiKeyError(f"no API key: add one to the Keychain with "
                      f"`security add-generic-password -a \"$USER\" -s {service} -w` "
                      f"(it prompts for the key), or set ANTHROPIC_API_KEY")


def redact(params: dict) -> dict:
    """The request with each image replaced by its SHA-256: what a recording
    keeps, and what a replay compares."""
    out = copy.deepcopy(params)
    for msg in out.get("messages", []):
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            src = block.get("source") if isinstance(block, dict) else None
            if block.get("type") == "image" and src and src.get("type") == "base64":
                raw = base64.standard_b64decode(src["data"])
                block["source"] = {"type": "base64", "media_type": src["media_type"],
                                   "sha256": hashlib.sha256(raw).hexdigest()}
    return out


def fingerprint(params: dict) -> str:
    """What the answer depends on: the request minus cache settings (a batch
    caches for an hour, a standard call for five minutes)."""
    p = redact(params)
    for block in p.get("system", []):
        block.pop("cache_control", None)
    return hashlib.sha256(json.dumps(p, sort_keys=True).encode()).hexdigest()


class Transport:
    live = False          # True when calls cost money: the cost guard applies
    supports_batch = True

    def create(self, key: str, params: dict, meta: dict) -> Outcome:
        raise NotImplementedError

    def batch_submit(self, items: list[tuple[str, dict, dict]]) -> str:
        raise NotImplementedError

    def batch_status(self, batch_id: str) -> dict:
        """{"status": "in_progress" | "canceling" | "ended", "counts": {...}}"""
        raise NotImplementedError

    def batch_results(self, batch_id: str):
        """Yields an Outcome per request, in any order."""
        raise NotImplementedError

    def batch_cancel(self, batch_id: str) -> dict:
        """Asks for the batch to stop. It goes to canceling, then ends; the
        requests that never ran come back as canceled (and are not billed)."""
        raise NotImplementedError


class AnthropicTransport(Transport):
    live = True

    def __init__(self, api_settings, key: str | None = None, http_client=None):
        import anthropic
        self._anthropic = anthropic
        self.settings = api_settings
        extra = {"http_client": http_client} if http_client is not None else {}
        self.client = anthropic.Anthropic(api_key=key or api_key(api_settings.keychain_service),
                                          max_retries=api_settings.max_retries, **extra)

    def _call(self, fn, *args, **kwargs):
        a = self._anthropic
        try:
            return fn(*args, **kwargs)
        except a.AuthenticationError:
            raise ApiError("the API rejected the key (401): check the Keychain entry") from None
        except a.PermissionDeniedError as e:
            raise ApiError(f"permission denied (403): {e.message}") from None
        except a.BadRequestError as e:
            raise ApiError(f"bad request (400): {e.message}") from None
        except a.RateLimitError:
            raise ApiError("rate limited (429) after retries; rerun later to resume") from None
        except a.APIStatusError as e:
            raise ApiError(f"API error {e.status_code}: {e.message}; rerun to resume") from None
        except a.APIConnectionError:
            raise ApiError("could not reach the API; rerun to resume") from None

    def create(self, key, params, meta):
        if self.settings.refusal_fallback:
            msg = self._call(self.client.beta.messages.create, **params,
                             betas=[FALLBACK_BETA], fallbacks="default")
        else:
            msg = self._call(self.client.messages.create, **params)
        return Outcome(key, msg.to_dict())

    def batch_submit(self, items):
        batch = self._call(self.client.messages.batches.create,
                           requests=[{"custom_id": key, "params": params} for key, params, _ in items])
        return batch.id

    def batch_status(self, batch_id):
        b = self._call(self.client.messages.batches.retrieve, batch_id)
        return {"status": b.processing_status, "counts": b.request_counts.to_dict()}

    def batch_cancel(self, batch_id):
        b = self._call(self.client.messages.batches.cancel, batch_id)
        return {"status": b.processing_status, "counts": b.request_counts.to_dict()}

    def batch_results(self, batch_id):
        for r in self._call(self.client.messages.batches.results, batch_id):
            res = r.result
            if res.type == "succeeded":
                yield Outcome(r.custom_id, res.message.to_dict(), mode="batch", batch_id=batch_id)
            else:
                detail = getattr(getattr(res, "error", None), "error", None)
                detail = getattr(detail, "message", "") if detail else ""
                yield Outcome(r.custom_id, None, f"{res.type} {detail}".strip(), mode="batch", batch_id=batch_id)


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


class RecordingTransport(Transport):
    """Saves DIR/calls/<key>.json for every request: the request (images
    replaced by their SHA-256), its fingerprint, and the response."""

    def __init__(self, inner: Transport, directory: Path):
        self.inner = inner
        self.dir = Path(directory)
        self.live = inner.live
        self.supports_batch = inner.supports_batch
        self.dir.mkdir(parents=True, exist_ok=True)
        info = self.dir / "recording.json"
        if not info.exists():
            _write_json(info, {"created": dt.datetime.now().isoformat(timespec="seconds"),
                               "format": 1, "transport": type(inner).__name__})

    def _path(self, key):
        return self.dir / "calls" / f"{key}.json"

    def _save(self, key, params, meta, outcome=None, mode="standard", batch_id=None):
        path = self._path(key)
        entry = json.loads(path.read_text()) if path.exists() and params is None else {}
        if params is not None:
            entry.update(key=key, meta=meta, request=redact(params), fingerprint=fingerprint(params))
        entry.update(mode=mode, batch_id=batch_id)
        if outcome is not None:
            entry.update(response=outcome.message, error=outcome.error)
        _write_json(path, entry)

    def create(self, key, params, meta):
        out = self.inner.create(key, params, meta)
        self._save(key, params, meta, out)
        return out

    def batch_submit(self, items):
        batch_id = self.inner.batch_submit(items)
        for key, params, meta in items:
            self._save(key, params, meta, mode="batch", batch_id=batch_id)
        return batch_id

    def batch_status(self, batch_id):
        return self.inner.batch_status(batch_id)

    def batch_cancel(self, batch_id):
        return self.inner.batch_cancel(batch_id)

    def batch_results(self, batch_id):
        for out in self.inner.batch_results(batch_id):
            self._save(out.key, None, None, out, mode="batch", batch_id=batch_id)
            yield out


class ReplayMissing(ApiError):
    pass


class ReplayTransport(Transport):
    """Answers every request from DIR/calls/<key>.json. Requests whose
    fingerprint differs from the recorded one (a prompt or a crop changed
    since) are answered anyway and listed in .stale. A batch ends at once."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        if not (self.dir / "calls").is_dir():
            raise ApiError(f"{self.dir} is not a recording (no calls/ folder)")
        self.stale: list[str] = []
        self._batches = {}

    def batch_submit(self, items):
        batch_id = f"replay-{len(self._batches) + 1}"
        self._batches[batch_id] = items
        return batch_id

    def batch_status(self, batch_id):
        if batch_id not in self._batches:
            raise ApiError(f"batch {batch_id} was submitted live and is not in this replay; "
                           "collect it with a live run first")
        return {"status": "ended", "counts": {"processing": 0}}

    def batch_results(self, batch_id):
        for key, params, meta in self._batches[batch_id]:
            yield self.create(key, params, meta)

    def create(self, key, params, meta):
        path = self.dir / "calls" / f"{key}.json"
        if not path.exists():
            raise ReplayMissing(f"the recording in {self.dir} has no response for {key}")
        entry = json.loads(path.read_text())
        if entry.get("fingerprint") != fingerprint(params):
            self.stale.append(key)
        if entry.get("response") is None and entry.get("error") is None:
            raise ReplayMissing(f"{key} was submitted in batch {entry.get('batch_id')} but never collected")
        return Outcome(key, entry.get("response"), entry.get("error"),
                       mode=entry.get("mode", "standard"), batch_id=entry.get("batch_id"))
