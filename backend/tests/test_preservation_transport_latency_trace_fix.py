"""Preservation tests for the /ws and HTTP transport surface (latency-trace-fix).

# Feature: latency-trace-fix, Property 2: Preservation

Covers the transport-observable preservation requirements from bugfix.md §3,
observed on the UNFIXED code and asserted here (these MUST PASS on unfixed):

- 3.5  /ws frame protocol: `{"text":...}` accepted, empty/malformed frames
       ignored without dropping the connection, incremental
       `{"type":"trace","entry":...}` events plus one
       `{"type":"response","message":...,"trace":[...]}` event.
- 3.10 POST /invocations: HTTP 200 with reply text + complete trace (incl. the
       CRM entry when invoked); a processing error returns a safe 200.
- 3.11 GET /ping -> {"status":"Healthy"}; GET /health -> runtime status with
       deployment mode + registered-agent count; POST /upload-document returns
       the stored file_key, or an error status that never claims success.

The FastAPI endpoints are async functions driven directly with ``asyncio.run``
plus lightweight fake ``WebSocket`` / ``Request`` / ``UploadFile`` doubles, so
no ASGI HTTP client (httpx) is required. All AWS boundaries are mocked (Bedrock
via a fake Orchestrator client injected onto the process-wide singletons;
DynamoDB via an injected fake table; S3 via a patched client factory). No real
AWS is contacted.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from fastapi import WebSocketDisconnect

import main
from bedrock_client import AgentResponse


# --------------------------------------------------------------------------- #
# Test doubles + wiring for the process-wide singletons
# --------------------------------------------------------------------------- #


class _FakeBedrockSelecting:
    """Fake Bedrock Runtime client the Orchestrator uses to pick an agent."""

    def __init__(self, chosen_agent: str) -> None:
        self._chosen = chosen_agent

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        selection = json.dumps({"agent": self._chosen, "reason": "matches"})
        return {"body": json.dumps({"content": [{"type": "text", "text": selection}]})}


class _FakeTable:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, *, Item: dict) -> dict:  # noqa: N803
        self.items.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class _FakeWebSocket:
    """Minimal WebSocket double recording sent frames and replaying inbound ones.

    ``inbound`` is the queue of frames the client "sends"; once exhausted the
    receive raises :class:`WebSocketDisconnect`, matching how the real handler
    ends its loop. ``sent`` records every frame the server sent, in order.
    """

    def __init__(self, inbound: list) -> None:
        self._inbound = list(inbound)
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self):
        if not self._inbound:
            raise WebSocketDisconnect(code=1000)
        return self._inbound.pop(0)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _FakeRequest:
    """Minimal Request double for /invocations (json body + headers)."""

    def __init__(self, body, headers=None) -> None:
        self._body = body
        self.headers = headers or {}

    async def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _FakeUploadFile:
    """Minimal UploadFile double for /upload-document."""

    def __init__(self, filename: str, content: bytes, content_type: str) -> None:
        self.filename = filename
        self._content = content
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._content


@pytest.fixture
def wired(monkeypatch):
    """Wire the process-wide Orchestrator/store and mock AWS for the app.

    Routes everything to `license_advisor` (a canned recommendation reply) and
    injects a fake DynamoDB table into the CRM agent so the auto-fire path runs
    without AWS.
    """
    from orchestrator import Orchestrator
    from session_store import InMemorySessionStore
    from agents import AGENT_REGISTRY
    import agents.license_advisor as la
    import agents.crm_agent as crm

    fake_table = _FakeTable()
    monkeypatch.setattr(crm, "_default_table", lambda: fake_table)
    monkeypatch.setattr(
        la, "invoke_agent",
        lambda **k: AgentResponse(
            text="The Startup package at AED 22,000/year fits your needs.",
            tools_used=["bedrock"],
            time_ms=4,
        ),
    )

    orch = Orchestrator(
        registry=AGENT_REGISTRY,
        bedrock=_FakeBedrockSelecting("license_advisor"),
        default_agent="license_advisor",
    )
    monkeypatch.setattr(main, "_orchestrator", orch)
    monkeypatch.setattr(main, "_session_store", InMemorySessionStore())
    return fake_table


# --------------------------------------------------------------------------- #
# 3.5 WebSocket frame protocol
# --------------------------------------------------------------------------- #


def test_ws_accepts_text_and_streams_trace_then_response(wired):
    """A {"text":...} frame yields incremental trace events + one response (3.5)."""
    ws = _FakeWebSocket([{"text": "I want the Startup package"}])
    asyncio.run(main.websocket_endpoint(ws, "session-a"))

    assert ws.accepted
    trace_events = [e for e in ws.sent if e.get("type") == "trace"]
    response_events = [e for e in ws.sent if e.get("type") == "response"]
    assert len(response_events) == 1
    assert len(trace_events) >= 1
    for e in trace_events:
        entry = e["entry"]
        assert {"agent_selected", "routing_reason", "tools_used", "time_ms"}.issubset(entry)
    # The response event carries message + trace, and is emitted BEFORE any CRM
    # auto-fire trace frame (the latency-trace-fix defect C2 / R1.3: the reply is
    # sent without waiting for the background CRM step, whose Trace_Entry streams
    # afterwards). So the response is the last of the *reply-path* frames, not
    # necessarily the last frame overall when a recommendation fires the CRM.
    response_frame = response_events[0]
    assert "message" in response_frame and isinstance(response_frame["trace"], list)
    response_idx = ws.sent.index(response_frame)
    crm_trace_idx = next(
        (
            i
            for i, f in enumerate(ws.sent)
            if f.get("type") == "trace"
            and f.get("entry", {}).get("agent_selected") == "crm_agent"
        ),
        None,
    )
    if crm_trace_idx is not None:
        assert response_idx < crm_trace_idx
    else:
        # No CRM auto-fire this turn -> the response is the final frame as before.
        assert ws.sent[-1]["type"] == "response"


@settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(bad=st.sampled_from([{}, {"text": ""}, {"text": "   "}, {"foo": "bar"}, {"text": 5}, "notadict"]))
def test_ws_ignores_empty_or_malformed_frames_without_dropping(bad, wired):
    """Empty/malformed frames are ignored; a valid frame afterwards still works (3.5)."""
    ws = _FakeWebSocket([bad, {"text": "recommend a package"}])
    asyncio.run(main.websocket_endpoint(ws, "session-b"))

    response_events = [e for e in ws.sent if e.get("type") == "response"]
    # Exactly one response — the malformed frame produced nothing, the valid one did.
    assert len(response_events) == 1
    assert isinstance(response_events[0]["message"], str)


def test_ws_malformed_frame_alone_sends_nothing(wired):
    """A lone malformed frame yields no frames but keeps the socket open (3.5)."""
    ws = _FakeWebSocket([{"foo": "bar"}])
    asyncio.run(main.websocket_endpoint(ws, "session-c"))
    assert ws.accepted
    assert ws.sent == []


# --------------------------------------------------------------------------- #
# 3.10 POST /invocations
# --------------------------------------------------------------------------- #


def test_invocations_returns_200_reply_and_full_trace(wired):
    """/invocations returns HTTP 200 with reply + complete trace incl. CRM (3.10)."""
    req = _FakeRequest({"text": "I want the Startup package"})
    resp = asyncio.run(main.invocations(req))
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert isinstance(body["message"], str) and body["message"]
    assert isinstance(body["trace"], list)
    agents_in_trace = [e["agent_selected"] for e in body["trace"]]
    assert "license_advisor" in agents_in_trace
    assert "crm_agent" in agents_in_trace  # auto-fire entry included
    # Report-only turn timings are surfaced on /invocations too, matching the
    # WebSocket payload (C1, R1.1/R1.2) — non-negative ints, never None.
    assert isinstance(body["routing_ms"], int) and body["routing_ms"] >= 0
    assert isinstance(body["total_ms"], int) and body["total_ms"] >= 0


def test_invocations_error_degrades_to_safe_200(monkeypatch):
    """A processing error returns a safe HTTP 200, never a 5xx (3.10)."""
    def _boom(*a, **k):
        raise RuntimeError("processing blew up")

    monkeypatch.setattr(main, "process_message", _boom)
    req = _FakeRequest({"text": "hi"})
    resp = asyncio.run(main.invocations(req))
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert isinstance(body["message"], str) and body["message"]
    assert body["trace"] == []
    # Even a degraded turn returns valid int timings (default 0), never None.
    assert body.get("routing_ms") == 0 and body.get("total_ms") == 0


# --------------------------------------------------------------------------- #
# 3.11 /ping, /health, /upload-document
# --------------------------------------------------------------------------- #


def test_ping_returns_healthy():
    """GET /ping returns exactly {"status":"Healthy"} (3.11)."""
    assert main.ping() == {"status": "Healthy"}


def test_health_reports_mode_and_agent_count():
    """GET /health reports deployment mode and registered-agent count (3.11)."""
    from agents import AGENT_REGISTRY
    from config import config

    body = main.health()
    assert body["status"] == "ok"
    assert body["deployment_mode"] == config.deployment_mode
    assert body["registered_agents"] == len(AGENT_REGISTRY)


def test_upload_document_returns_file_key(monkeypatch):
    """POST /upload-document stores the file and returns its file_key (3.11)."""
    class _FakeS3:
        def __init__(self):
            self.puts = []

        def put_object(self, **kwargs):
            self.puts.append(kwargs)

    fake_s3 = _FakeS3()
    monkeypatch.setattr(main, "_default_s3_client", lambda: fake_s3)

    upload = _FakeUploadFile("passport.pdf", b"binary-bytes", "application/pdf")
    resp = asyncio.run(main.upload_document(upload))
    assert resp.status_code == 200
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert body["file_key"] == "uploads/passport.pdf"
    assert len(fake_s3.puts) == 1


def test_upload_document_failure_never_claims_success(monkeypatch):
    """An S3 failure returns an error status that never claims success (3.11)."""
    class _FailingS3:
        def put_object(self, **kwargs):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(main, "_default_s3_client", lambda: _FailingS3())

    upload = _FakeUploadFile("passport.pdf", b"bytes", "application/pdf")
    resp = asyncio.run(main.upload_document(upload))
    assert resp.status_code == 500
    body = json.loads(bytes(resp.body).decode("utf-8"))
    assert "file_key" not in body
    assert "error" in body
