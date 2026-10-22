"""The live transport, through the real Anthropic SDK, against a mocked HTTP
layer: what goes over the wire, and how responses and batch results come
back. No network and no key are involved."""

import json
import subprocess

import anthropic
import cv2
import httpx2
import numpy as np
import pytest

from grading import apiclient, config, prompts
from grading.apiclient import AnthropicTransport, ApiKeyError, RecordingTransport

KEY = "sk-ant-test-0123456789"
MESSAGE = {"id": "msg_01", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
           "content": [{"type": "tool_use", "id": "toolu_01", "name": "record_reading",
                        "input": {"reading": "x = 3/2", "latex": "x = \\frac{3}{2}", "confidence": "high",
                                  "illegible": False}}],
           "stop_reason": "tool_use", "stop_sequence": None,
           "usage": {"input_tokens": 120, "output_tokens": 80, "cache_creation_input_tokens": 0,
                     "cache_read_input_tokens": 900}}
BATCH = {"id": "msgbatch_01", "type": "message_batch", "processing_status": "in_progress",
         "request_counts": {"processing": 2, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
         "created_at": "2026-10-22T12:00:00Z", "expires_at": "2026-10-23T12:00:00Z", "ended_at": None,
         "cancel_initiated_at": None, "archived_at": None, "results_url": None}


class Server:
    def __init__(self):
        self.requests = []

    def __call__(self, request: httpx2.Request):
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/messages":
            return httpx2.Response(200, json=MESSAGE)
        if path == "/v1/messages/batches" and request.method == "POST":
            return httpx2.Response(200, json=BATCH)
        if path == "/v1/messages/batches/msgbatch_01":
            ended = dict(BATCH, processing_status="ended", ended_at="2026-10-22T12:30:00Z",
                         request_counts=dict(BATCH["request_counts"], processing=0, succeeded=1, errored=1),
                         results_url="https://api.anthropic.com/v1/messages/batches/msgbatch_01/results")
            return httpx2.Response(200, json=ended)
        if path == "/v1/messages/batches/msgbatch_01/cancel" and request.method == "POST":
            return httpx2.Response(200, json=dict(BATCH, processing_status="canceling",
                                                  cancel_initiated_at="2026-10-22T14:04:00Z"))
        if path == "/v1/messages/batches/msgbatch_01/results":
            lines = [{"custom_id": "read-b", "result": {"type": "errored", "error": {
                         "type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}}},
                     {"custom_id": "read-a", "result": {"type": "succeeded", "message": MESSAGE}},
                     {"custom_id": "read-c", "result": {"type": "canceled"}}]
            return httpx2.Response(200, text="\n".join(json.dumps(x) for x in lines),
                                   headers={"content-type": "application/x-jsonl"})
        return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": path}})


@pytest.fixture
def server():
    return Server()


def transport(server, **api):
    settings = config.ApiSettings(**api)
    return AnthropicTransport(settings, key=KEY, http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(server)))


def png():
    ok, buf = cv2.imencode(".png", np.full((60, 200), 255, np.uint8))
    return buf.tobytes()


def params(ttl=None):
    q = {"id": "gs6", "type": "short", "points": 1}
    return prompts.read_request(config.ApiSettings(), q, png(), ttl)


def test_standard_call_goes_out_as_documented(server):
    out = transport(server).create("read-a", params(), {})
    assert out.message["content"][0]["input"]["reading"] == "x = 3/2" and out.mode == "standard"
    (req,) = server.requests
    body = json.loads(req.content)
    # SDK 2.x sends the key as a bearer token, on API version 2026-10-21.
    assert KEY in req.headers.get("authorization", "") + req.headers.get("x-api-key", "")
    # Refusal fallback: the "default" form and its beta header.
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in req.headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5" and body["output_config"] == {"effort": "low"}
    (tool,) = body["tools"]
    assert tool["strict"] is True and tool["input_schema"]["additionalProperties"] is False
    assert body["tool_choice"] == {"type": "auto", "disable_parallel_tool_use": True}
    assert body["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(body)
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[1]["type"] == "text"


def test_without_fallback_the_plain_endpoint(server):
    transport(server, refusal_fallback=False).create("read-a", params(), {})
    (req,) = server.requests
    assert "fallbacks" not in json.loads(req.content)
    assert "server-side-fallback" not in req.headers.get("anthropic-beta", "")


def test_batch_submit_status_and_results(server):
    t = transport(server)
    batch_id = t.batch_submit([("read-a", params("1h"), {}), ("read-b", params("1h"), {})])
    assert batch_id == "msgbatch_01"
    body = json.loads(server.requests[0].content)
    assert [r["custom_id"] for r in body["requests"]] == ["read-a", "read-b"]
    first = body["requests"][0]["params"]
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "fallbacks" not in first  # the Batch API rejects it
    assert t.batch_status(batch_id)["status"] == "ended"
    results = {o.key: o for o in t.batch_results(batch_id)}
    assert results["read-a"].message["usage"]["cache_read_input_tokens"] == 900
    assert results["read-a"].mode == "batch" and results["read-a"].batch_id == "msgbatch_01"
    assert results["read-b"].message is None and results["read-b"].error.startswith("errored")
    assert results["read-c"].message is None and results["read-c"].error == "canceled"


def test_batch_cancel_goes_out_as_documented(server):
    st = transport(server).batch_cancel("msgbatch_01")
    (req,) = server.requests
    assert req.method == "POST" and req.url.path == "/v1/messages/batches/msgbatch_01/cancel"
    assert st["status"] == "canceling" and st["counts"]["processing"] == 2


def test_recording_passes_a_cancel_through(server, tmp_path):
    rec = RecordingTransport(transport(server), tmp_path / "rec")
    assert rec.batch_cancel("msgbatch_01")["status"] == "canceling"


def test_api_errors_become_messages(server):
    def bad(request):
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error",
                                                                     "message": "tool_choice not supported"}})
    t = AnthropicTransport(config.ApiSettings(), key=KEY, http_client=anthropic.DefaultHttpxClient(
        transport=httpx2.MockTransport(bad)))
    with pytest.raises(apiclient.ApiError, match="bad request.*tool_choice"):
        t.create("read-a", params(), {})


def test_recording_never_holds_the_key(server, tmp_path):
    rec = RecordingTransport(transport(server), tmp_path / "rec")
    rec.create("read-a", params(), {"call_type": "read"})
    rec.batch_submit([("read-b", params("1h"), {"call_type": "read"})])
    list(rec.batch_results("msgbatch_01"))
    for f in (tmp_path / "rec").rglob("*.json"):
        assert KEY not in f.read_text()


def test_key_comes_from_the_keychain_first(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=KEY + "\n", stderr="")
    monkeypatch.setattr(apiclient.subprocess, "run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert apiclient.api_key("anthropic-grading") == KEY
    assert calls == [["security", "find-generic-password", "-s", "anthropic-grading", "-w"]]


def test_key_falls_back_to_the_environment(monkeypatch, capsys):
    def no_security(cmd, **kw):
        raise FileNotFoundError("security")
    monkeypatch.setattr(apiclient.subprocess, "run", no_security)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert apiclient.api_key("anthropic-grading") == "from-env"
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(ApiKeyError, match="add-generic-password"):
        apiclient.api_key("anthropic-grading")
    out = capsys.readouterr()
    assert "from-env" not in out.out + out.err


def test_keychain_miss_falls_back_to_the_environment(monkeypatch):
    def not_found(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 44, stdout="", stderr="could not be found")
    monkeypatch.setattr(apiclient.subprocess, "run", not_found)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert apiclient.api_key("anthropic-grading") == "from-env"
