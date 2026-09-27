"""Bug-condition exploration tests for the latency-trace-fix spec (Task 1).

# Feature: latency-trace-fix, Property 1: Bug Condition

These tests encode the EXPECTED POST-FIX behavior for the six latency/trace
defects (C1-C6) described in ``.kiro/specs/latency-trace-fix/bugfix.md`` and
``design.md`` "Exploratory Bug Condition Checking".

CRITICAL: They are written to FAIL on the UNFIXED code. Each failure is a
concrete counterexample proving the corresponding defect exists. They will pass
once the fix (task 3) is implemented; do NOT change the code or these tests to
make them pass at the exploration stage.

Scoped property-based approach: for these deterministic defects we scope each
"for all" property to concrete, reproducible failing cases -- a greeting
message, a recommendation message, and a slow-Bedrock stub -- so the
counterexamples are stable.

All AWS boundaries (Bedrock, DynamoDB, S3) are mocked with injected fakes /
stubs / call recorders. Nothing here touches real AWS or the network. The async
WebSocket handler and ``/ping`` are driven directly on a private event loop with
a fake WebSocket (no TestClient / httpx dependency).

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Any, Optional

import pytest

import bedrock_client
import main
import orchestrator as orchestrator_mod
from bedrock_client import AgentResponse
from orchestrator import Orchestrator, RoutingDecision


# --------------------------------------------------------------------------- #
# Fakes: DynamoDB table, session store, orchestrator, WebSocket
# --------------------------------------------------------------------------- #


class FakeTable:
    """A stand-in DynamoDB Table that records ``put_item`` calls in memory."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, *, Item: dict) -> dict:  # noqa: N803 - boto3 kwarg name
        self.items.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


class FakeSessionStore:
    """Minimal in-memory session store matching the SessionStore protocol.

    ``append_message_turns`` (backend/session_store.py) calls
    ``store.append_turn(session_id, role, content)`` twice per processed
    message, so this fake implements ``get_history`` + ``append_turn``.
    """

    def __init__(self) -> None:
        self.turns: dict[str, list[dict]] = {}

    def get_history(self, session_id: str) -> list[dict]:
        return list(self.turns.get(session_id, []))

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        self.turns.setdefault(session_id, []).append({"role": role, "content": content})


class FakeOrchestrator:
    """A router that always routes to a fixed agent with a fixed reason."""

    def __init__(self, agent: str = "license_advisor", reason: str = "test routing") -> None:
        self._agent = agent
        self._reason = reason
        self.route_calls = 0

    def route(
        self, user_message: str, history: list[dict], last_agent: str | None = None
    ) -> RoutingDecision:
        # Mirrors Orchestrator.route's context-aware signature (last_agent is a
        # defaulted parameter the runtime now threads in). This fake ignores it.
        self.route_calls += 1
        return RoutingDecision(agent=self._agent, routing_reason=self._reason)


class FakeWebSocket:
    """A fake Starlette WebSocket: feeds queued inbound frames, records sends.

    ``receive_json`` yields the queued inbound frames in order, then raises
    ``WebSocketDisconnect`` to end the handler loop the way a client close does.
    Each ``send_json`` records the frame together with a monotonic timestamp so
    ordering between ``trace`` and ``response`` frames is observable.
    """

    def __init__(self, inbound: list[dict]) -> None:
        self._inbound = list(inbound)
        self.accepted = False
        self.sent: list[dict] = []
        self.sent_at: list[float] = []

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        if self._inbound:
            return self._inbound.pop(0)
        from fastapi import WebSocketDisconnect

        raise WebSocketDisconnect(code=1000)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)
        self.sent_at.append(time.perf_counter())


# --------------------------------------------------------------------------- #
# Registries wiring a controllable license_advisor + crm_agent
# --------------------------------------------------------------------------- #


def make_registry(
    la_handler,
    crm_table: Optional[FakeTable] = None,
):
    """Build an agent registry with the real crm_agent and a given LA handler."""
    import agents.crm_agent as crm

    table = crm_table if crm_table is not None else FakeTable()

    def crm_handler(user_message: str, history=None):  # noqa: ANN001
        return crm.handle(user_message, history, table=table)

    return {
        "license_advisor": {"description": "LA", "handler": la_handler},
        "crm_agent": {"description": "CRM", "handler": crm_handler},
    }, table


def greeting_la_handler(user_message: str, history=None) -> AgentResponse:  # noqa: ANN001
    """License_Advisor reply that is NOT a recommendation (a greeting/clarify)."""
    return AgentResponse(
        text="Hello! To help pick a package, what will your company do, "
        "how many visas, and when do you want to launch?",
        tools_used=["bedrock"],
        time_ms=5,
    )


def recommendation_la_handler(user_message: str, history=None) -> AgentResponse:  # noqa: ANN001
    """License_Advisor reply that IS a recommendation (one package + AED cost)."""
    return AgentResponse(
        text="I recommend the Startup package at AED 22,000/year for three "
        "visas and a fast launch.",
        tools_used=["bedrock"],
        time_ms=5,
        structured_data={
            "recommended_package": "Startup",
            "annual_cost_aed": 22000,
            "reason": "three visas + fast launch",
        },
    )


def run_ws_turn(inbound: list[dict], registry, store, orch) -> FakeWebSocket:
    """Drive the async ``/ws`` handler to completion on a private event loop.

    Patches ``process_message`` inputs via ``get_orchestrator``/``get_session_store``
    is avoided by monkeypatching in the test; here we rely on the handler calling
    the module-level ``process_message`` which reads those singletons. The test
    installs the fakes before calling this.
    """
    ws = FakeWebSocket(inbound)

    async def _drive():
        await main.websocket_endpoint(ws, "sess-1")

    asyncio.run(_drive())
    return ws


# --------------------------------------------------------------------------- #
# Fixtures: install fakes for the singletons used by process_message / handlers
# --------------------------------------------------------------------------- #


@pytest.fixture
def install_fakes(monkeypatch):
    """Return a helper that installs a registry/store/orchestrator for a test.

    Patches ``main.get_session_store``/``get_orchestrator`` and the module-level
    ``AGENT_REGISTRY`` lookups (``main.process_message`` imports AGENT_REGISTRY
    from ``agents`` at call time) so the fakes are honored end to end.
    """

    def _install(registry, store, orch):
        monkeypatch.setattr(main, "get_session_store", lambda: store)
        monkeypatch.setattr(main, "get_orchestrator", lambda: orch)

        # process_message imports `from agents import AGENT_REGISTRY` inside the
        # function; patch the live registry dict contents in place so the
        # injected handlers are used, then restore is automatic via monkeypatch.
        import agents

        monkeypatch.setattr(agents, "AGENT_REGISTRY", registry, raising=True)
        # runtime._registry() also reads `from agents import AGENT_REGISTRY`.
        # Patching the attribute above covers both import sites.

    return _install


# --------------------------------------------------------------------------- #
# C1 -- Missing timings (Requirements 1.1, 1.2)
# --------------------------------------------------------------------------- #


def test_c1_response_reports_routing_and_total_time(install_fakes):
    """C1: the response payload must carry routing time and total turn time.

    FAILS today: process_message returns only {type, message, trace} -- there
    are no routing_ms / total_ms fields, so the trace under-reports the wait.
    """
    store = FakeSessionStore()
    orch = FakeOrchestrator(agent="license_advisor")
    registry, _table = make_registry(greeting_la_handler)
    install_fakes(registry, store, orch)

    trace_events: list[dict] = []
    result = main.process_message(
        "sess-c1", "hello", trace_events.append, orchestrator=orch, store=store
    )

    # A routing time (>= 0 int) must be reported alongside the trace (R1.1),
    # measured even on a routing fallback.
    assert (
        result.get("routing_ms") is not None
    ), "C1: response payload is missing routing_ms (routing time not reported)"
    # A total turn time (>= 0 int) must be reported (R1.2).
    assert (
        result.get("total_ms") is not None
    ), "C1: response payload is missing total_ms (total turn time not reported)"


# --------------------------------------------------------------------------- #
# C2 -- Foreground CRM + reply pollution (Requirements 1.3, 1.5)
# --------------------------------------------------------------------------- #


_LEAD_LINE = re.compile(r"Lead\s+LEAD-\d+\s+captured", re.IGNORECASE)


def test_c2_recommendation_reply_has_no_lead_line_and_precedes_crm_trace(install_fakes):
    """C2: on a recommendation the reply must not contain the CRM confirmation,
    and the reply frame must be sent before the CRM trace frame.

    FAILS today: crm_auto_fire runs in the foreground inside process_message and
    its "Lead LEAD-... captured" text is appended to the reply, and the whole
    trace (including the CRM entry) is buffered and flushed before the single
    response frame -- so no reply is emitted before the CRM trace.
    """
    store = FakeSessionStore()
    orch = FakeOrchestrator(agent="license_advisor")
    registry, table = make_registry(recommendation_la_handler)
    install_fakes(registry, store, orch)

    ws = run_ws_turn([{"text": "startup, 3 visas, next month"}], registry, store, orch)

    # Locate the response frame and the CRM trace frame among the sent frames.
    response_idx = next(
        (i for i, f in enumerate(ws.sent) if f.get("type") == "response"), None
    )
    assert response_idx is not None, "C2: no response frame was sent"
    reply_text = ws.sent[response_idx].get("message", "")

    # (R1.5) The CRM confirmation must not pollute the reply text.
    assert not _LEAD_LINE.search(reply_text), (
        "C2: reply text contains the CRM 'Lead ... captured' line: " + repr(reply_text)
    )

    # (R1.3) The reply must be emitted before the CRM Trace_Entry frame.
    crm_trace_idx = next(
        (
            i
            for i, f in enumerate(ws.sent)
            if f.get("type") == "trace"
            and f.get("entry", {}).get("agent_selected") == "crm_agent"
        ),
        None,
    )
    assert crm_trace_idx is not None, "C2: no CRM trace frame was sent for a recommendation"
    assert response_idx < crm_trace_idx, (
        "C2: reply frame was sent AFTER the CRM trace frame "
        f"(response at {response_idx}, crm trace at {crm_trace_idx}) -- "
        "CRM ran in the foreground and delayed the reply"
    )


# --------------------------------------------------------------------------- #
# C6 -- CRM on a non-recommendation (Requirements 1.4)
# --------------------------------------------------------------------------- #


def test_c6_greeting_does_not_fire_crm(install_fakes):
    """C6: a greeting routed to license_advisor must not write a lead, must not
    produce a CRM Trace_Entry, and must not add CRM text to the reply.

    FAILS today: crm_auto_fire fires on ANY license_advisor turn, so a greeting
    writes a lead, emits a CRM trace, and appends the CRM confirmation.
    """
    store = FakeSessionStore()
    orch = FakeOrchestrator(agent="license_advisor")
    registry, table = make_registry(greeting_la_handler)
    install_fakes(registry, store, orch)

    trace_events: list[dict] = []
    result = main.process_message(
        "sess-c6", "hello", trace_events.append, orchestrator=orch, store=store
    )

    # No lead written to DynamoDB for a non-recommendation.
    assert table.items == [], (
        "C6: a lead was written to DynamoDB for a greeting (non-recommendation): "
        + repr(table.items)
    )

    # No CRM Trace_Entry produced.
    crm_entries = [
        e
        for e in result.get("trace", [])
        if e.get("agent_selected") == "crm_agent"
    ]
    assert crm_entries == [], "C6: a CRM Trace_Entry was produced for a greeting"

    # No CRM confirmation text in the reply.
    assert not _LEAD_LINE.search(result.get("message", "")), (
        "C6: the reply for a greeting contains the CRM 'Lead ... captured' line"
    )


# --------------------------------------------------------------------------- #
# C3 -- AWS client churn (Requirements 1.6)
# --------------------------------------------------------------------------- #


def test_c3_bedrock_client_factory_invoked_once_across_calls(monkeypatch):
    """C3: the AWS client factory must be invoked once and the client reused.

    FAILS today: bedrock_client.invoke_agent calls _default_bedrock_client() on
    every call, so processing several messages rebuilds the client each time.
    """
    calls = {"n": 0}

    class _StubClient:
        def invoke_model(self, *, modelId, body):  # noqa: N803
            import json

            payload = {"content": [{"type": "text", "text": "ok response"}]}
            return {"body": json.dumps(payload)}

    def counting_factory():
        calls["n"] += 1
        return _StubClient()

    monkeypatch.setattr(bedrock_client, "_default_bedrock_client", counting_factory)

    # Drive several agent invocations through the real invoke_agent path with no
    # injected client, so it must resolve the factory each time it needs one.
    for _ in range(3):
        bedrock_client.invoke_agent(
            system_prompt="s",
            user_message="hi",
            tools_used=["bedrock"],
            fallback_text="fb",
            timeout_s=10.0,
        )

    assert calls["n"] == 1, (
        "C3: AWS client factory was invoked "
        f"{calls['n']} times across 3 calls (expected 1 -- client should be reused)"
    )


# --------------------------------------------------------------------------- #
# C4 -- Budgets not enforced (Requirements 1.7, 1.8)
# --------------------------------------------------------------------------- #


def test_c4_agent_degrades_within_one_second_of_budget(monkeypatch):
    """C4 (agent budget, R1.7): a slow Bedrock call must be cut off no more than
    ~1s after the agent's time budget, degrading to the fallback.

    Scoped case: a Bedrock stub that sleeps far beyond a small (scaled) budget.
    FAILS today: invoke_agent ignores timeout_s, so it waits for the whole sleep
    instead of degrading within budget + 1s.
    """
    budget_s = 1.0
    sleep_s = 6.0  # well beyond budget + 1s

    class _SlowClient:
        def invoke_model(self, *, modelId, body):  # noqa: N803
            time.sleep(sleep_s)
            import json

            payload = {"content": [{"type": "text", "text": "late response"}]}
            return {"body": json.dumps(payload)}

    start = time.perf_counter()
    resp = bedrock_client.invoke_agent(
        system_prompt="s",
        user_message="hi",
        tools_used=["bedrock"],
        fallback_text="FALLBACK",
        timeout_s=budget_s,
        bedrock_client=_SlowClient(),
    )
    elapsed = time.perf_counter() - start

    assert elapsed <= budget_s + 1.0, (
        "C4: agent Bedrock call was not cut off within budget + 1s "
        f"(elapsed {elapsed:.2f}s, budget {budget_s}s) -- timeout_s not enforced"
    )
    assert resp.get("tools_used") == ["fallback-mode"], (
        "C4: agent did not degrade to the fallback after exceeding its budget"
    )


def test_c4_routing_degrades_within_one_second_of_budget(monkeypatch):
    """C4 (routing budget, R1.8): the routing call must fall back no more than
    ~1s after the 30s budget, returning REASON_TIMEOUT.

    Scoped case: a Sonnet stub that sleeps beyond a small (scaled) routing
    budget. FAILS today: _invoke_sonnet's ThreadPoolExecutor context manager
    blocks on shutdown(wait=True) after future.result times out, so route()
    returns only after the stuck call finishes -- far past budget + 1s.
    """
    budget_s = 1.0
    sleep_s = 6.0
    monkeypatch.setattr(orchestrator_mod, "ROUTING_TIMEOUT_S", budget_s)

    class _SlowSonnet:
        def invoke_model(self, *, modelId, body):  # noqa: N803
            time.sleep(sleep_s)
            import json

            selection = json.dumps({"agent": "license_advisor", "reason": "late"})
            payload = {"content": [{"type": "text", "text": selection}]}
            return {"body": json.dumps(payload)}

    orch = Orchestrator(
        registry={"license_advisor": {"description": "LA", "handler": greeting_la_handler}},
        bedrock=_SlowSonnet(),
        default_agent="license_advisor",
    )

    start = time.perf_counter()
    decision = orch.route("hello", [])
    elapsed = time.perf_counter() - start

    assert elapsed <= budget_s + 1.0, (
        "C4: routing did not fall back within budget + 1s "
        f"(elapsed {elapsed:.2f}s, budget {budget_s}s) -- executor blocks on shutdown"
    )
    assert decision.routing_reason == orchestrator_mod.REASON_TIMEOUT, (
        "C4: routing fallback reason was not the timeout reason"
    )


# --------------------------------------------------------------------------- #
# C5 -- Buffered trace / blocked event loop (Requirements 1.9, 1.10)
# --------------------------------------------------------------------------- #


def test_c5_trace_streams_incrementally_and_loop_stays_responsive(install_fakes):
    """C5: trace frames must stream as each step completes (R1.9), and the event
    loop must keep serving other work while a turn is in flight (R1.10).

    Scoped case: a license_advisor handler that blocks for ~2s. The /ws handler
    runs as an asyncio task while a concurrent "ticker" coroutine increments a
    counter every 10ms on the same loop, standing in for another connection /
    the /ping probe that must stay responsive.

    Two independent counterexamples are collected:

    * loop responsiveness (R1.10): the number of ticker increments observed
      DURING the ~2s turn. On a responsive loop the ticker advances many times;
      on the unfixed handler the synchronous process_message monopolizes the
      single loop thread, so the ticker cannot advance until the turn ends.
    * live trace (R1.9): whether the first trace frame is SENT before the turn's
      blocking work finishes. On the unfixed handler every frame is buffered in
      `pending` and flushed only after process_message returns, so the first
      send timestamp is at/after turn completion, never mid-turn.
    """
    store = FakeSessionStore()
    orch = FakeOrchestrator(agent="license_advisor")

    turn_done = threading.Event()

    def slow_la_handler(user_message: str, history=None):  # noqa: ANN001
        time.sleep(2.0)
        turn_done.set()
        return AgentResponse(
            text="Here are some general licensing options to consider.",
            tools_used=["bedrock"],
            time_ms=5,
        )

    registry, _table = make_registry(slow_la_handler)
    install_fakes(registry, store, orch)

    ws = FakeWebSocket([{"text": "tell me about licensing"}])

    async def _scenario():
        ticks = {"n": 0}

        async def ticker():
            # Advance a counter roughly every 10ms until the turn completes.
            while not turn_done.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0.01)

        ticker_task = asyncio.create_task(ticker())
        handler_task = asyncio.create_task(main.websocket_endpoint(ws, "sess-c5"))

        await handler_task
        # Snapshot ticks observed by the time the turn finished, then stop.
        ticks_during_turn = ticks["n"]
        turn_done.set()
        await ticker_task
        return ticks_during_turn

    turn_start = time.perf_counter()
    ticks_during_turn = asyncio.run(_scenario())

    # When was the FIRST frame sent, relative to the turn's blocking work?
    first_send_at = ws.sent_at[0] if ws.sent_at else float("inf")
    first_send_offset = first_send_at - turn_start

    # (R1.10) A ~2s turn allows ~200 ticks at 10ms cadence on a responsive loop.
    # Require clearly more than a couple: the unfixed handler blocks the loop so
    # the ticker gets ~0-1 ticks in before the turn releases the thread.
    assert ticks_during_turn >= 10, (
        "C5: the event loop was blocked during the turn -- a concurrent task "
        f"advanced only {ticks_during_turn} times over a ~2s turn "
        "(process_message ran synchronously on the loop)"
    )

    # (R1.9) The first trace frame must be sent well before the ~2s turn ends;
    # the unfixed handler buffers all frames and flushes them only after the turn.
    assert first_send_offset < 1.5, (
        "C5: the first frame was sent "
        f"{first_send_offset:.2f}s into a ~2s turn -- trace is buffered until the "
        "turn finishes instead of streaming live as each step completes"
    )
