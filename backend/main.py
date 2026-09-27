"""WebSocket / HTTP surface for the Ask Gulf runtime (design §8, R11).

This module wires the previously-built runtime pieces together behind a FastAPI
application — the deployable surface that gets packaged into the AgentCore
Runtime container (Task 18). It exposes three endpoints from design §8:

* ``/ws/{session_id}`` (R11.1) — the persistent WebSocket the frontend uses for
  real-time messaging and live trace updates. Per message the runtime loads the
  session history, runs the Orchestrator to select a specialist, invokes that
  agent, then runs the CRM auto-fire hook and the chaining engine. Every agent
  invocation produces a well-formed ``Trace_Entry`` that is (a) logged (R1.11)
  and (b) streamed to the client immediately as a ``{"type":"trace","entry":..}``
  event (R11.2, R11.4). After all invocations a final
  ``{"type":"response","message":..,"trace":[..]}`` event carries the
  concatenated text and the full accumulated trace, and the user + assistant
  turns are appended to the session store.

* ``POST /upload-document`` — stores an uploaded file in the
  ``ask-gulf-documents`` S3 bucket and returns its ``file_key`` for the future
  Document Verifier. On failure it returns an HTTP error status and never
  claims success (design §8, R14 upload path).

* ``GET /health`` — lightweight runtime health for local/Docker checks.

* ``GET /ping`` and ``POST /invocations`` — the Amazon Bedrock AgentCore
  Runtime HTTP service contract. AgentCore probes ``GET /ping`` (expecting
  ``{"status":"Healthy"}``) to decide the container is healthy, and delivers
  each user turn as ``POST /invocations`` carrying the session id in the
  ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` header. Both reuse the same
  :func:`process_message` machinery as the WebSocket handler, so tracing /
  auto-fire / chaining behave identically regardless of transport.

The Orchestrator is a *router*: it only selects an agent (returning a
``RoutingDecision``); this module resolves the handler from ``AGENT_REGISTRY``
and invokes it, so the same trace / auto-fire / chaining machinery wraps every
invocation uniformly (design §1, §5, §6). The primary agent's ``Trace_Entry``
carries the Orchestrator's ``routing_reason``; the CRM auto-fire and each chain
step carry their own reasons via :mod:`runtime`.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import threading
import time
import uuid
from typing import Any, Callable, Optional

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from bedrock_client import get_bedrock_client, validate_response
from config import config
from orchestrator import Orchestrator
from runtime import build_trace_entry, crm_auto_fire, run_chain
from session_store import append_message_turns, create_session_store

logger = logging.getLogger("ask_gulf.runtime")

# A Trace_Entry is a plain dict; an emit callback receives each one as produced.
TraceEntry = dict[str, Any]
EmitTrace = Callable[[TraceEntry], None]


# ---------------------------------------------------------------------------
# Lazy singletons
# ---------------------------------------------------------------------------
#
# The Bedrock client, Orchestrator, and session store are created on first use
# rather than at import time so that importing this module never forces boto3 /
# live AWS credentials (keeping the module import-safe for tests and tooling).

_orchestrator: Optional[Orchestrator] = None
_session_store = None


def get_orchestrator() -> Orchestrator:
    """Return the process-wide Orchestrator, constructing it on first use.

    The Orchestrator reads the live ``AGENT_REGISTRY`` at decision time (R2.5),
    so it is safe to build once and reuse. Its Bedrock Runtime client is created
    with the shared, SSO-profile-aware helper from :mod:`bedrock_client`
    (no secrets in source, R16).
    """
    global _orchestrator
    if _orchestrator is None:
        from agents import AGENT_REGISTRY

        _orchestrator = Orchestrator(
            registry=AGENT_REGISTRY,
            bedrock=get_bedrock_client(),
            default_agent=config.default_agent,
        )
    return _orchestrator


def get_session_store():
    """Return the process-wide session store selected by ``deployment_mode``.

    ``local`` -> in-memory store; ``agentcore`` -> AgentCore Memory
    (R16.2, R16.3, R18.3). Constructed on first use.
    """
    global _session_store
    if _session_store is None:
        _session_store = create_session_store(config)
    return _session_store


# ---------------------------------------------------------------------------
# S3 upload client seam
# ---------------------------------------------------------------------------


def _default_s3_client():
    """Create an S3 client using ambient (non-secret) credentials.

    Mirrors :func:`bedrock_client._default_bedrock_client`: locally it resolves
    credentials from the SSO profile, and in the deployed environment from the
    AgentCore execution role (R16). Imported lazily so importing this module
    never requires boto3.

    This factory is the single point at which the S3 credential source is
    resolved; :func:`get_s3_client` calls through here exactly once and caches
    the result, so the reuse (C3, R1.6) does not change the credential source.
    """
    import boto3  # local import keeps the module import-safe without AWS

    if config.aws_sso_profile:
        session = boto3.Session(profile_name=config.aws_sso_profile)
        return session.client("s3", region_name=config.aws_region)
    return boto3.client("s3", region_name=config.aws_region)


# Process-wide, thread-safe S3 client reuse (C3). A boto3 low-level client is
# thread-safe once created, so the upload path builds one per process, lazily,
# behind a lock (double-checked) and reuses it instead of rebuilding it on every
# upload. The cache calls through :func:`_default_s3_client`, preserving the
# credential source (3.13); only the reuse is new.
_s3_client_singleton: Optional[Any] = None
_s3_client_lock = threading.Lock()


def get_s3_client() -> Any:
    """Return the process-wide S3 client, building it once (C3, R1.6).

    Lazily constructs the client via :func:`_default_s3_client` on first use and
    reuses it thereafter. Thread-safe via double-checked locking so concurrent
    first-use never races or builds twice.
    """
    global _s3_client_singleton
    client = _s3_client_singleton
    if client is None:
        with _s3_client_lock:
            client = _s3_client_singleton
            if client is None:
                client = _default_s3_client()
                _s3_client_singleton = client
    return client


def reset_s3_client_cache() -> None:
    """Clear the cached S3 client (test seam; not used in production)."""
    global _s3_client_singleton
    with _s3_client_lock:
        _s3_client_singleton = None


# ---------------------------------------------------------------------------
# Per-session last-selected-agent memory (context-aware routing)
# ---------------------------------------------------------------------------
#
# Routing is context-aware: the Orchestrator keeps a context-dependent follow-up
# with the agent that produced the previous assistant turn (a clarifying
# question or an application-field request) unless the user clearly changes
# topic. The runtime is the component that knows which agent was selected each
# turn — the session store persists turns as ``{role, content}`` with no agent
# attribution (a contract we do not change) — so the runtime remembers the
# last-selected agent per session here and passes it into ``route()``.
#
# A plain dict guarded by a lock, mirroring the S3 client reuse pattern above.
# Bounded implicitly by the number of live sessions; entries are overwritten
# each turn. Never touches AWS.
_last_agent_by_session: dict[str, str] = {}
_last_agent_lock = threading.Lock()


def get_last_agent(session_id: str) -> Optional[str]:
    """Return the agent selected on this session's previous turn, or None."""
    with _last_agent_lock:
        return _last_agent_by_session.get(session_id)


def set_last_agent(session_id: str, agent: str) -> None:
    """Remember the agent selected for this session's current turn."""
    with _last_agent_lock:
        _last_agent_by_session[session_id] = agent


def reset_last_agent_cache() -> None:
    """Clear the per-session last-agent memory (test seam; not used in prod)."""
    with _last_agent_lock:
        _last_agent_by_session.clear()


# ---------------------------------------------------------------------------
# Core per-message processing (framework-agnostic)
# ---------------------------------------------------------------------------


def process_message(
    session_id: str,
    user_message: str,
    emit_trace: EmitTrace,
    *,
    orchestrator: Optional[Orchestrator] = None,
    store=None,
) -> dict:
    """Run one user message end-to-end, emitting a trace per agent invocation.

    This is the heart of the WebSocket / HTTP handlers, factored out so it is
    testable without a live socket. Following design §8 "Message Flow", it is
    split into two phases (task 3.6):

    * a FOREGROUND (reply-path) phase — :func:`run_foreground` — that loads
      history, routes, invokes + validates the primary agent, records the
      primary ``Trace_Entry``, runs the chaining engine, builds the final reply
      text, and appends the history turns. This phase does NOT run the CRM
      auto-fire.
    * a BACKGROUND CRM phase — :func:`run_background_crm` — that, gated on a
      genuine License Advisor recommendation, fires ``crm_agent`` for the same
      message and records its ``Trace_Entry`` (R7.1, R7.5). Its confirmation
      text is not merged into the reply (defect C2).

    ``process_message`` invokes the CRM phase SYNCHRONOUSLY right after the
    foreground phase, so observable behavior is unchanged from today: the CRM
    still fires for a recommendation and its ``Trace_Entry`` still appears in the
    returned ``trace`` and via ``emit_trace``. The split is structural — the
    transport layer (task 3.7) will move that CRM call to run after the reply is
    sent / off the loop so the CRM no longer delays the reply.

    Every emitted ``Trace_Entry`` is logged (R1.11) and passed to
    ``emit_trace`` so the WebSocket layer streams it to the client the instant
    it is produced (R11.2, R11.4).

    Args:
        session_id: the conversation/session identifier.
        user_message: the incoming user message text.
        emit_trace: callback receiving each ``Trace_Entry`` as produced.
        orchestrator: the router to use; defaults to the process-wide one.
        store: the session store to use; defaults to the process-wide one.

    Returns:
        The final ``{"type":"response","message":..,"trace":[..],
        "routing_ms":int,"total_ms":int}`` payload. ``routing_ms`` (C1/R1.1) is
        how long routing took, measured even on a routing fallback; ``total_ms``
        (C1/R1.2) is the total time to produce the reply, excluding the
        background CRM work. Both are report-only additive fields — not
        Trace_Entry records — so the trace shape is unchanged (3.4).
    """
    orch = orchestrator if orchestrator is not None else get_orchestrator()
    session = store if store is not None else get_session_store()

    # Stamp the start of the turn with a monotonic clock so the total turn time
    # (what the user waits for the reply) can be reported alongside the trace
    # (C1, R1.2). The CRM auto-fire is deliberately excluded from total_ms — it
    # is background work that does not gate the reply (defect C2).
    _turn_start = time.perf_counter()

    # Accumulate every Trace_Entry so the final response carries the full trace
    # (R11.4); each is also streamed live as it is produced.
    trace: list[TraceEntry] = []

    def _record(entry: TraceEntry) -> None:
        """Log the entry (R1.11), accumulate it, and stream it live (R11.2)."""
        logger.info(
            "Trace_Entry agent=%s tools=%s time_ms=%s reason=%s",
            entry.get("agent_selected"),
            entry.get("tools_used"),
            entry.get("time_ms"),
            entry.get("routing_reason"),
        )
        trace.append(entry)
        emit_trace({"type": "trace", "entry": entry})

    # --- FOREGROUND phase: everything on the reply path (no CRM). -----------
    foreground = run_foreground(
        session_id,
        user_message,
        _record,
        orchestrator=orch,
        store=session,
    )

    # The reply is ready once the foreground phase completes; the total turn
    # time is measured up to here, excluding the background CRM work below
    # (defect C2). Coerce to a non-negative int (report-only, C1/R1.2).
    total_ms = max(0, int((time.perf_counter() - _turn_start) * 1000))
    routing_ms = foreground.get("routing_ms", 0)
    logger.info(
        "turn timings session=%s routing_ms=%s total_ms=%s",
        session_id,
        routing_ms,
        total_ms,
    )

    # --- CRM phase: run the separately-callable background CRM unit. --------
    #
    # The CRM auto-fire lives in its own independently-invocable function
    # (:func:`run_background_crm`) so the transport layer (task 3.7) can move
    # this call to run AFTER the reply is sent / off the loop. For now
    # ``process_message`` invokes it SYNCHRONOUSLY, immediately after the
    # foreground phase, so the observable behavior is unchanged: the CRM still
    # fires for a genuine recommendation and its Trace_Entry is still recorded
    # into ``trace`` and streamed via ``emit_trace`` (defect C2 gating from 3.5
    # is preserved; only the code structure changed here for 3.6).
    run_background_crm(
        foreground["decision_agent"],
        user_message,
        foreground["history"],
        _record,
        primary_response=foreground["primary_response"],
    )

    # Carry routing_ms and total_ms as report-only additive fields on the final
    # response payload (C1, R1.1/R1.2). They are NOT Trace_Entry records, so the
    # Trace_Entry shape (3.4) is unchanged; the existing message/trace keys are
    # preserved so the /invocations and /ws transports (3.10/3.5) still work.
    return {
        "type": "response",
        "message": foreground["message"],
        "trace": trace,
        "routing_ms": routing_ms,
        "total_ms": total_ms,
    }


def run_foreground(
    session_id: str,
    user_message: str,
    record: EmitTrace,
    *,
    orchestrator: Optional[Orchestrator] = None,
    store=None,
) -> dict:
    """Run the FOREGROUND (reply-path) phase of one user message (task 3.6).

    This is everything that must complete before the user-facing reply can be
    emitted: load history, route, invoke + validate the primary agent, record
    the primary ``Trace_Entry``, run the chaining engine, build the final reply
    text, and append the history turns. It deliberately does NOT run the CRM
    auto-fire — that lives in :func:`run_background_crm` so the transport layer
    can schedule it off the reply path (task 3.7).

    Every ``Trace_Entry`` is passed to ``record`` (the caller's log + accumulate
    + live-stream callback) so streaming behavior is unchanged (R11.2).

    Args:
        session_id: the conversation/session identifier.
        user_message: the incoming user message text.
        record: callback receiving each foreground ``Trace_Entry`` as produced.
        orchestrator: the router to use; defaults to the process-wide one.
        store: the session store to use; defaults to the process-wide one.

    Returns:
        A dict carrying the reply and the context the CRM phase needs:
        ``{"message": str, "decision_agent": str, "primary_response": dict|None,
        "history": list, "routing_ms": int}``. ``history`` is the history
        snapshot loaded for this turn (the CRM auto-fire runs against the same
        snapshot, not the post-append history). ``routing_ms`` is how long the
        Orchestrator's routing decision took (report-only, C1/R1.1), measured
        even on a routing fallback.
    """
    orch = orchestrator if orchestrator is not None else get_orchestrator()
    session = store if store is not None else get_session_store()

    # 1. Load history for this session (empty for an unseen session_id).
    history = session.get_history(session_id)

    # 2. Route — the Orchestrator only selects (R1.9, R1.10). It never raises;
    #    any anomaly degrades to the default agent (R1.5-R1.8). Time the whole
    #    decision with a monotonic clock so the routing wait is reported even
    #    when routing falls back (timeout/malformed/etc.) (C1, R1.1). The route
    #    contract is unchanged (agent/routing_reason preserved for 3.1/3.4).
    #    Thread the agent that produced this session's previous assistant turn
    #    into the routing decision so a context-dependent follow-up stays with
    #    the in-progress agent (context-aware routing). ``last_agent`` is None on
    #    the first turn, so first-turn routing is unchanged (3.7). The route
    #    contract is unchanged (agent/routing_reason preserved for 3.1/3.4).
    last_agent = get_last_agent(session_id)
    _route_start = time.perf_counter()
    decision = orch.route(user_message, history, last_agent=last_agent)
    routing_ms = max(0, int((time.perf_counter() - _route_start) * 1000))

    # Remember this turn's selected agent so the NEXT turn of this session can
    # recognise an in-progress collection flow (context-aware routing).
    set_last_agent(session_id, decision.agent)

    # 3. Resolve and invoke the primary agent. The decision always names an
    #    agent present in the registry snapshot used for the decision.
    from agents import AGENT_REGISTRY

    texts: list[str] = []
    primary_response: Optional[dict] = None
    record_entry = AGENT_REGISTRY.get(decision.agent)
    if record_entry is not None:
        primary_response = record_entry["handler"](user_message, history)
        violation = validate_response(primary_response)
        if violation is None:
            # Build the primary Trace_Entry from the routing_reason + the
            # response's tools_used/time_ms (R1.11); log + stream it (R11.2).
            record(
                build_trace_entry(
                    decision.agent,
                    primary_response,
                    routing_reason=decision.routing_reason,
                )
            )
            texts.append(primary_response["text"])
        else:
            # A contract-violating primary response is rejected: it contributes
            # no text and no trace, and history is left unchanged for it (R3.7).
            logger.warning(
                "primary agent %s produced an invalid response: %s",
                decision.agent,
                violation,
            )
            primary_response = None

    # 4. Chaining engine (R9). Follow the primary response's chain_next, emitting
    #    a trace per step; a missing/absent next agent degrades gracefully.
    if primary_response is not None:
        chain_next = primary_response.get("chain_next")
        if isinstance(chain_next, str) and chain_next:
            chain_steps = run_chain(chain_next, user_message, history, record)
            texts.extend(step.get("text", "") for step in chain_steps)

    # 5. Concatenate all response texts into the final message (design §8).
    final_message = "\n\n".join(t for t in texts if t)

    # 6. Append exactly one user turn and one assistant turn (R18.3). Only
    #    persist when there is an assistant message to record. The background
    #    CRM phase deliberately appends nothing to history (3.6).
    if final_message:
        append_message_turns(session, session_id, user_message, final_message)

    return {
        "message": final_message,
        "decision_agent": decision.agent,
        "primary_response": primary_response,
        "history": history,
        "routing_ms": routing_ms,
    }


def run_background_crm(
    selected_agent: str,
    user_message: str,
    history: Optional[list[dict]],
    record: EmitTrace,
    *,
    primary_response: Optional[dict] = None,
) -> Optional[dict]:
    """Run the CRM auto-fire as a separate, independently-invocable unit (3.6).

    This is the BACKGROUND phase extracted from :func:`process_message`. Given
    the selected agent, the user message, the turn's history snapshot, the
    primary response, and an emit/record callback, it runs the CRM auto-fire
    hook (:func:`runtime.crm_auto_fire`, gated on a genuine recommendation as
    task 3.5 established) and emits the CRM ``Trace_Entry`` through ``record``.

    It is a no-op (returns ``None``, records nothing) unless the primary agent
    was the License Advisor AND its reply was a genuine recommendation
    (defect C6). The CRM confirmation text is deliberately NOT merged into the
    reply (defect C2) — lead capture surfaces solely through the CRM
    ``Trace_Entry`` recorded here.

    ``process_message`` calls this synchronously today so observable behavior is
    unchanged (the CRM entry still appears in the returned trace). The split is
    structural: the transport layer (task 3.7) will call this AFTER the reply is
    sent / off the loop to take the CRM off the reply path.

    Args:
        selected_agent: the agent the Orchestrator selected for this message.
        user_message: the user message, passed unchanged to the CRM agent.
        history: the turn's history snapshot, passed unchanged to the CRM agent.
        record: callback receiving the CRM ``Trace_Entry`` when produced.
        primary_response: the primary License Advisor response; the hook gates
            on its ``is_recommendation`` signal.

    Returns:
        The ``crm_agent`` response dict when the hook fired and produced a valid
        response, otherwise ``None``.
    """
    return crm_auto_fire(
        selected_agent,
        user_message,
        history,
        record,
        primary_response=primary_response,
    )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(title="Ask Gulf Runtime")


@app.get("/health")
def health() -> dict:
    """Return runtime health for local/Docker checks (design §8).

    AgentCore itself probes ``GET /ping``; this richer endpoint is kept for
    local development and the Docker HEALTHCHECK.

    Reports the deployment mode and the number of registered agents so a health
    probe can confirm the registry initialized. Never touches Bedrock/AWS so it
    stays fast and dependency-free.
    """
    from agents import AGENT_REGISTRY

    return {
        "status": "ok",
        "deployment_mode": config.deployment_mode,
        "registered_agents": len(AGENT_REGISTRY),
    }


# ---------------------------------------------------------------------------
# Amazon Bedrock AgentCore Runtime HTTP service contract
# ---------------------------------------------------------------------------
#
# AgentCore's container contract requires the image to serve, on 0.0.0.0:8080:
#   GET  /ping        -> 200 {"status":"Healthy"}   (health probe)
#   POST /invocations -> 200 <response JSON payload> (one user turn)
# and injects the per-session header below for session isolation. A container
# that does not honour this contract never becomes healthy in the runtime.

# The header AgentCore injects to isolate one caller's session from another.
_AGENTCORE_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"

# Body fields accepted as the user's message text, in priority order.
_INVOCATION_TEXT_FIELDS = ("text", "prompt", "message")


def _extract_user_message(body: Any) -> str:
    """Pull the user message text out of a flexible ``/invocations`` body.

    Accepts a ``text`` / ``prompt`` / ``message`` field on the top-level object
    or on a nested ``input`` object; otherwise falls back to stringifying the
    body so a caller is never silently dropped.
    """
    if isinstance(body, dict):
        for field_name in _INVOCATION_TEXT_FIELDS:
            value = body.get(field_name)
            if isinstance(value, str) and value.strip():
                return value
        nested = body.get("input")
        if isinstance(nested, dict):
            for field_name in _INVOCATION_TEXT_FIELDS:
                value = nested.get(field_name)
                if isinstance(value, str) and value.strip():
                    return value
        elif isinstance(nested, str) and nested.strip():
            return nested
    if isinstance(body, str):
        return body
    if body is None:
        return ""
    return str(body)


@app.get("/ping")
def ping() -> dict:
    """AgentCore Runtime health probe (must return ``{"status":"Healthy"}``).

    AgentCore polls this endpoint to decide the container is ready; the exact
    payload shape is part of the contract, so it is intentionally fixed and
    dependency-free (never touches Bedrock/AWS).
    """
    return {"status": "Healthy"}


@app.post("/invocations")
async def invocations(request: Request) -> JSONResponse:
    """AgentCore Runtime message entrypoint (one user turn per call).

    Reads a flexible JSON body (``text`` / ``prompt`` / ``message`` at the top
    level or under ``input``, else the stringified body), resolves the session
    id from the ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id`` header (falling
    back to a body ``session_id`` field, then a generated uuid), and runs
    :func:`process_message` with a synchronous ``emit_trace`` that buffers each
    ``Trace_Entry`` into a list.

    Returns ``{"message": <final text>, "trace": [...]}`` with HTTP 200. Because
    AgentCore treats any non-200 as a failed invocation, a processing error
    degrades to a 200 carrying a safe fallback message rather than surfacing a
    5xx; only a failure to even read the request degrades to 500.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - empty/invalid body is tolerated
        body = None

    user_message = _extract_user_message(body)

    # Session id: header first (AgentCore-injected), then body, then a uuid.
    session_id = request.headers.get(_AGENTCORE_SESSION_HEADER) or ""
    if not session_id and isinstance(body, dict):
        candidate = body.get("session_id")
        if isinstance(candidate, str) and candidate.strip():
            session_id = candidate
    if not session_id:
        session_id = str(uuid.uuid4())

    # Buffer trace events synchronously; the final payload carries the full
    # trace so an /invocations caller sees the same detail as the WebSocket.
    pending: list[dict] = []

    def _emit(event: dict) -> None:
        pending.append(event)

    try:
        result = process_message(session_id, user_message, _emit)
    except Exception as exc:  # noqa: BLE001 - AgentCore treats non-200 as failure
        logger.exception("invocations failed for session=%s", session_id)
        # Degrade to a 200 with a safe message rather than a 5xx so a single
        # bad turn does not mark the whole runtime unhealthy.
        return JSONResponse(
            status_code=200,
            content={
                "message": (
                    "Sorry, something went wrong while processing that request. "
                    "Please try again."
                ),
                "trace": [],
                # Keep the /invocations payload shape uniform on the error
                # path too: timings are 0 (not absent) for a degraded turn.
                "routing_ms": 0,
                "total_ms": 0,
                "error": str(exc),
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "message": result.get("message", ""),
            "trace": result.get("trace", []),
            # Report-only turn timings (C1, R1.1/R1.2). Surfaced on /invocations
            # too so this transport matches the WebSocket payload; default to 0
            # (not None) so a degraded turn still returns valid ints.
            "routing_ms": result.get("routing_ms", 0),
            "total_ms": result.get("total_ms", 0),
        },
    )


@app.post("/upload-document")
async def upload_document(file: UploadFile = File(...)) -> JSONResponse:
    """Store an uploaded file in S3 and return its ``file_key`` (design §8).

    Puts the file into the ``ask-gulf-documents`` bucket (``config.s3_bucket``)
    for the future Document Verifier. On any failure it returns an HTTP 500
    error status and never reports success (R14 upload path). The ``file_key``
    is namespaced under ``uploads/`` and preserves the client filename.

    Args:
        file: the multipart-uploaded file.

    Returns:
        ``{"file_key": ...}`` with 200 on success, or an error body with 500.
    """
    file_key = f"uploads/{file.filename}"
    try:
        contents = await file.read()
        s3 = get_s3_client()
        s3.put_object(
            Bucket=config.s3_bucket,
            Key=file_key,
            Body=contents,
            ContentType=file.content_type or "application/octet-stream",
        )
    except Exception as exc:  # noqa: BLE001 - surface any failure as an error status
        logger.exception("upload-document failed for %s", file.filename)
        # Do not claim success: return an error status (R14 upload path).
        return JSONResponse(
            status_code=500,
            content={"error": "upload failed", "detail": str(exc)},
        )

    return JSONResponse(status_code=200, content={"file_key": file_key})


# Per-turn correlation id generator (additive field, 3.7). Each WebSocket turn
# gets a monotonic id so a late CRM trace frame can be reconciled to its message
# on the client after the next message is already on the wire.
_ws_turn_counter = itertools.count(1)


async def _run_ws_turn(
    websocket: WebSocket,
    session_id: str,
    user_message: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Run one WebSocket turn: off-loop work, live trace, reply-before-CRM.

    Design of this turn (task 3.7, defects C2 + C5):

    * OFF-LOOP (R1.10): the blocking foreground work (:func:`run_foreground`)
      and the blocking CRM work (:func:`run_background_crm`) each run in the
      default thread-pool executor via ``loop.run_in_executor`` so the single
      event-loop thread stays free to serve other connections / the ``/ping``
      probe while a slow turn is in flight.

    * LIVE STREAM (R1.9): the worker thread's synchronous ``emit_trace`` callback
      cannot touch the socket directly, so it hands each event off to the loop
      thread by pushing onto an ``asyncio.Queue`` via
      ``loop.call_soon_threadsafe(queue.put_nowait, event)``. A serialized
      drainer coroutine ``await``\\s items off the queue and ``await``\\s
      ``ws.send_json`` for each, so every frame goes out the instant its step
      completes rather than being buffered until the turn ends.

    * REPLY BEFORE CRM (C2/R1.3): the foreground runs first; its trace frames
      drain live, then the ``response`` frame is sent, and ONLY THEN is the CRM
      step run (also off-loop) with its ``Trace_Entry`` streamed afterwards. A
      single serialized sender guarantees the reply and the possibly-later CRM
      trace never interleave on the one socket.
    """
    turn_id = next(_ws_turn_counter)
    queue: asyncio.Queue[dict] = asyncio.Queue()
    # Sentinel object pushed onto the queue AFTER a worker's last emit so the
    # drainer knows the phase is complete once it has forwarded every frame
    # (avoids racing the executor future against still-pending emit callbacks).
    _SENTINEL = object()

    # Accumulate every foreground Trace_Entry so the final ``response`` frame can
    # carry the full trace (R11.4), matching the buffered path's payload shape
    # (3.5 asserts the response frame's ``trace`` is a list). Recorded from the
    # worker thread; appends are ordered and the list is only read after the
    # foreground future completes, so no lock is needed.
    foreground_trace: list[dict] = []

    def _emit(entry: dict) -> None:
        # Called from the worker thread with a raw Trace_Entry (run_foreground /
        # run_background_crm pass the bare entry, exactly as process_message's
        # own recorder receives it). Log it (R1.11), accumulate it for the
        # response payload, then wrap it as a ``{"type":"trace","entry":..}``
        # frame (tagged with the per-turn correlation id — additive, 3.5) and
        # hand it to the loop thread so the drainer sends it live and in order.
        logger.info(
            "Trace_Entry agent=%s tools=%s time_ms=%s reason=%s",
            entry.get("agent_selected"),
            entry.get("tools_used"),
            entry.get("time_ms"),
            entry.get("routing_reason"),
        )
        foreground_trace.append(entry)
        frame = {"type": "trace", "entry": entry, "turn_id": turn_id}
        loop.call_soon_threadsafe(queue.put_nowait, frame)

    async def _drain_phase(worker: "asyncio.Future") -> None:
        """Run ``worker`` off-loop and forward each frame it emits, live.

        The blocking ``worker`` (a ``run_in_executor`` future) is scheduled, then
        a callback pushes a sentinel onto the queue when it finishes. The drainer
        ``await``\\s frames off the queue and ``await``\\s ``ws.send_json`` for
        each the instant it arrives (R1.9), stopping when it reaches the
        sentinel. Because the sentinel is enqueued via the same loop-thread hand
        off as the emits, every frame is guaranteed forwarded before the phase
        ends — no frame is dropped and the coroutine terminates deterministically
        (so the enclosing ``asyncio.run`` returns under a fake WebSocket).
        """
        worker.add_done_callback(
            lambda _f: loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)
        )
        while True:
            event = await queue.get()
            if event is _SENTINEL:
                # Forward any frames the worker emitted right before finishing
                # that landed after we picked up the sentinel is impossible
                # (sentinel is enqueued last), so the queue holds only frames
                # already accounted for; stop here.
                break
            await websocket.send_json(event)
        # Surface any worker exception (foreground never raises; CRM is guarded).
        await worker

    # --- FOREGROUND (reply path): run off-loop, stream its trace live. -------
    # Stamp the turn start so total_ms (C1/R1.2) covers the reply path only,
    # excluding the background CRM work below.
    turn_start = time.perf_counter()

    # Acknowledge the turn the instant it is accepted, BEFORE dispatching the
    # (potentially slow) foreground work off-loop. This additive
    # ``{"type":"status",...}`` frame streams live at the start of the turn so
    # the client sees the runtime is working during a long agent call rather
    # than waiting in silence until the first Trace_Entry lands (R1.9). It is a
    # new, optional frame type: the trace/response protocol (3.5) is unchanged,
    # and consumers that only handle ``trace``/``response`` simply skip it.
    await websocket.send_json(
        {"type": "status", "state": "processing", "turn_id": turn_id}
    )

    foreground_future = loop.run_in_executor(
        None,
        lambda: run_foreground(session_id, user_message, _emit),
    )
    await _drain_phase(foreground_future)
    foreground = foreground_future.result()

    # No reply produced (e.g. contract-rejected primary): nothing to send and no
    # CRM to fire. Matches the buffered path's "send nothing" for such turns.
    if not foreground.get("message"):
        return

    # --- REPLY: send the response frame BEFORE any CRM trace (C2/R1.3). ------
    total_ms = max(0, int((time.perf_counter() - turn_start) * 1000))
    await websocket.send_json(
        {
            "type": "response",
            "message": foreground["message"],
            "trace": list(foreground_trace),
            "routing_ms": foreground.get("routing_ms", 0),
            "total_ms": total_ms,
            "turn_id": turn_id,
        }
    )

    # --- BACKGROUND CRM: run off-loop AFTER the reply; stream its trace. -----
    # Guard so a CRM failure is logged and swallowed — it never fails the reply
    # (already sent) nor closes the socket. The lead write happens inside the
    # worker thread and is detached from the send, so it completes even if the
    # client has since disconnected.
    def _run_crm() -> None:
        try:
            run_background_crm(
                foreground["decision_agent"],
                user_message,
                foreground["history"],
                _emit,
                primary_response=foreground["primary_response"],
            )
        except Exception:  # noqa: BLE001 - background CRM must never break the turn
            logger.exception(
                "background CRM failed for session=%s turn=%s", session_id, turn_id
            )

    crm_future = loop.run_in_executor(None, _run_crm)
    await _drain_phase(crm_future)


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str) -> None:
    """Real-time messaging + live trace channel (design §8, R11.1).

    Accepts the connection, then for each inbound ``{"text": ...}`` message runs
    the turn via :func:`_run_ws_turn`, which runs the blocking work OFF the event
    loop (so a slow turn never blocks other connections / ``/ping`` — R1.10),
    streams each ``{"type":"trace","entry":..}`` event the instant it is produced
    (R1.9), sends the ``{"type":"response",..}`` frame, and only THEN runs the
    CRM auto-fire so its trace frame follows the reply (C2/R1.3). Empty/malformed
    frames are ignored without dropping the connection (3.5).
    """
    await websocket.accept()
    loop = asyncio.get_running_loop()
    try:
        while True:
            payload = await websocket.receive_json()
            user_message = payload.get("text", "") if isinstance(payload, dict) else ""
            if not isinstance(user_message, str) or not user_message.strip():
                # Ignore empty/malformed frames without dropping the connection.
                continue

            await _run_ws_turn(websocket, session_id, user_message, loop)
    except WebSocketDisconnect:
        logger.info("websocket disconnected: session=%s", session_id)
