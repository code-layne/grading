"""Talking to the Claude API, or to a recording of it.

A transport sends one request (`create`) or a batch of them (`batch_submit`,
`batch_status`, `batch_results`, `batch_cancel`) and hands back plain dicts shaped like the
API's own JSON, so the pipeline never cares which transport it has:

  AnthropicTransport  the live API through the Anthropic Python SDK
  RecordingTransport  wraps another transport and saves every request and response
  ReplayTransport     answers from a recording, with no network
  fake.FakeTransport  answers from expected-scores.csv (tests and `eval --fake`)

`create` is called from several threads at once (standard.py). It raises
RateLimited or Retryable when sending the request again may work; the
pipeline does the retrying.

The API key comes from the macOS Keychain (`security find-generic-password
-s anthropic-grading -w`), else $ANTHROPIC_API_KEY. It is handed to the SDK
and nowhere else: never printed, logged or recorded.
"""

import base64
import copy
import datetime as dt
import email.utils
import hashlib
import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ApiError(Exception):
    pass


class ApiKeyError(ApiError):
    pass


class Retryable(ApiError):
    """A standard call that may succeed if sent again: a connection error, a
    timeout, a 5xx. retry_after is the server's Retry-After in seconds, if any."""

    def __init__(self, message, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimited(Retryable):
    """A 429 or an overloaded error (529): wait retry_after, then send it
    again with fewer calls in flight."""


def retry_after(headers) -> float | None:
    """Seconds to wait from retry-after-ms or retry-after (seconds or an HTTP date)."""
    if headers is None:
        return None
    for name, scale in (("retry-after-ms", 1000.0), ("retry-after", 1.0)):
        raw = headers.get(name)
        if raw is None:
            continue
        try:
            return max(float(raw) / scale, 0.0)
        except ValueError:
            pass
        if name == "retry-after":
            try:
                when = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                return None
            return max((when - dt.datetime.now(dt.timezone.utc)).total_seconds(), 0.0)
    return None


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
        """One standard call. Called from several threads at once. Raises
        RateLimited or Retryable when sending it again may work."""
        raise NotImplementedError

    def finish(self, complete: bool):
        """The run is over: complete when it left nothing still to send."""

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
        """A Batch API call: the SDK has already retried what it could."""
        try:
            return fn(*args, **kwargs)
        except self._anthropic.APIError as e:
            err = self._error(e)
            if isinstance(err, Retryable):
                err = ApiError(f"{err} after retries; rerun to resume")
            raise err from None

    def _error(self, e) -> ApiError:
        a = self._anthropic
        if isinstance(e, a.APIStatusError):
            wait = retry_after(e.response.headers)
            if e.status_code in (429, 529) or "overloaded_error" in str(e.body):
                return RateLimited(f"{'rate limited' if e.status_code == 429 else 'overloaded'} "
                                   f"({e.status_code})", wait)
            if e.status_code in (408, 409) or e.status_code >= 500:
                return Retryable(f"API error {e.status_code}: {e.message}", wait)
        if isinstance(e, a.AuthenticationError):
            return ApiError("the API rejected the key (401): check the Keychain entry")
        if isinstance(e, a.PermissionDeniedError):
            return ApiError(f"permission denied (403): {e.message}")
        if isinstance(e, a.BadRequestError):
            return ApiError(f"bad request (400): {e.message}")
        if isinstance(e, a.APIStatusError):
            return ApiError(f"API error {e.status_code}: {e.message}; rerun to resume")
        if isinstance(e, a.APIConnectionError):
            return Retryable("could not reach the API")
        return ApiError(str(e))

    def create(self, key, params, meta):
        # No retries inside the SDK: the pipeline retries (standard.send), so
        # that a 429 also lowers how many calls it keeps in flight.
        client = self.client.with_options(max_retries=0)
        try:
            if self.settings.refusal_fallback:
                msg = client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
            else:
                msg = client.messages.create(**params)
        except self._anthropic.APIError as e:
            raise self._error(e) from None
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
    tmp = path.with_name(path.name + f".{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(path)


class RecordingTransport(Transport):
    """Saves DIR/calls/<key>.json for every request: the request (images
    replaced by their SHA-256), its fingerprint, and the response. A call's
    file is written the moment it returns, from the thread that made it.

    recording.json says whether the recording is complete. It is marked
    incomplete as a run starts and complete only when a run ends with nothing
    left to send, so a run cut short (Ctrl-C, a crash, a transient failure)
    leaves a recording that replays the calls it made and leaves the rest
    waiting. Recording into the same folder again adds to it."""

    def __init__(self, inner: Transport, directory: Path):
        self.inner = inner
        self.dir = Path(directory)
        self.live = inner.live
        self.supports_batch = inner.supports_batch
        self.dir.mkdir(parents=True, exist_ok=True)
        self._info(complete=False)

    def _info(self, complete: bool):
        path = self.dir / "recording.json"
        info = json.loads(path.read_text()) if path.exists() else {
            "created": dt.datetime.now().isoformat(timespec="seconds"), "format": 1,
            "transport": type(self.inner).__name__}
        info["complete"] = complete
        _write_json(path, info)

    def finish(self, complete):
        self.inner.finish(complete)
        self._info(complete)

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
    since) are answered anyway and listed in .stale. A batch ends at once.

    A request the recording has no response for is an error, unless
    recording.json marks the recording incomplete (a run cut short): then it
    comes back as `not-recorded`, which leaves the item waiting, the way the
    run that made the recording left it."""

    def __init__(self, directory: Path):
        self.dir = Path(directory)
        if not (self.dir / "calls").is_dir():
            raise ApiError(f"{self.dir} is not a recording (no calls/ folder)")
        info = self.dir / "recording.json"
        self.complete = json.loads(info.read_text()).get("complete", True) if info.exists() else True
        self.stale: list[str] = []
        self.missing: list[str] = []
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

    def _missing(self, key, why):
        if self.complete:
            raise ReplayMissing(why)
        self.missing.append(key)
        return Outcome(key, None, "not-recorded")

    def create(self, key, params, meta):
        path = self.dir / "calls" / f"{key}.json"
        if not path.exists():
            return self._missing(key, f"the recording in {self.dir} has no response for {key}")
        entry = json.loads(path.read_text())
        if entry.get("fingerprint") != fingerprint(params):
            self.stale.append(key)
        if entry.get("response") is None and entry.get("error") is None:
            return self._missing(key, f"{key} was submitted in batch {entry.get('batch_id')} but never collected")
        return Outcome(key, entry.get("response"), entry.get("error"),
                       mode=entry.get("mode", "standard"), batch_id=entry.get("batch_id"))
