"""Preservation property tests for the latency-trace-fix bugfix.

# Feature: latency-trace-fix, Property 2: Preservation

Property 2 (design.md): for any input where the bug condition does NOT hold,
the fixed system SHALL produce the same result as the original system. This
file follows the **observation-first** methodology required by tasks.md task 2:
we ran the UNFIXED code first, recorded its outputs, and assert those recorded
outputs here. These tests MUST PASS on the UNFIXED code (they lock in the
baseline behavior to preserve across requirements 3.1-3.13).

All AWS boundaries (Bedrock, DynamoDB, S3) are mocked with injected fakes /
stubs so the tests never call real AWS. Backend tests run with
``.\\.venv\\Scripts\\python.exe -m pytest`` from ``backend/`` (conftest.py puts
``backend/`` on sys.path).

Requirement map (bugfix.md §3 "Unchanged Behavior"):
- 3.1  routing agent/reason incl. the four exact fallback reasons
- 3.2  contract-rejection: no text, no Trace_Entry
- 3.3  degradation to Fallback_Response with tools_used ["fallback-mode"]
- 3.4  Trace_Entry shape (agent_selected, routing_reason, tools_used list,
       non-negative int time_ms for only that step)
- 3.5  /ws frame protocol (text accepted, empty/malformed ignored, incremental
       trace events + one response event)
- 3.6  history-turn accounting (one user + one assistant turn)
- 3.7  chaining behavior
- 3.8  recommendation flow (two entries + one NEW LEAD-<timestamp> lead)
- 3.9  non-License_Advisor routing (no auto-fire)
- 3.10 /invocations response
- 3.11 /ping /health /upload-document responses
- 3.12 per-agent user-visible rules (License_Advisor package/cost/149-word cap)
- 3.13 credential source (SSO profile locally, default chain deployed)
"""

from __future__ import annotations

import json
from typing import Optional

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st

import bedrock_client
import main
import runtime
from bedrock_client import AgentResponse, validate_response
from orchestrator import (
    MAX_REASON_CHARS,
    REASON_MALFORMED,
    REASON_NO_MATCH,
    REASON_TIMEOUT,
    REASON_UNKNOWN_NAME,
    Orchestrator,
    RoutingDecision,
)
from session_store import InMemorySessionStore


# --------------------------------------------------------------------------- #
# Shared test doubles (no real AWS)
# --------------------------------------------------------------------------- #


class _FakeBedrockSelecting:
    """Fake Bedrock Runtime client whose routing choice/reason is fixed.

    Emulates the Anthropic messages envelope the Orchestrator parses, returning
    strict JSON ``{"agent": ..., "reason": ...}`` as the assistant text.
    """

    def __init__(self, chosen_agent: str, reason: str = "matches the request") -> None:
        self._chosen = chosen_agent
        self._reason = reason

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        selection = json.dumps({"agent": self._chosen, "reason": self._reason})
        payload = {"content": [{"type": "text", "text": selection}]}
        return {"body": json.dumps(payload)}


class _FakeBedrockRaw:
    """Fake Bedrock client returning an arbitrary raw assistant text."""

    def __init__(self, raw_text: str) -> None:
        self._raw = raw_text

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        payload = {"content": [{"type": "text", "text": self._raw}]}
        return {"body": json.dumps(payload)}


class _FakeBedrockRaising:
    """Fake Bedrock client that raises on invoke_model (routing error path)."""

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        raise RuntimeError("bedrock unavailable")


class _FakeTable:
    """In-memory DynamoDB Table recording put_item calls."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, *, Item: dict) -> dict:  # noqa: N803 - boto3 kwarg name
        self.items.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _agent_registry(handlers: dict) -> dict:
    """Build a registry dict shaped like AGENT_REGISTRY from name->handler."""
    return {
        name: {"description": f"desc for {name}", "handler": handler}
        for name, handler in handlers.items()
    }


def _license_handler(text: str, *, tools=None, structured=None, chain_next=None):
    """Make a license_advisor-like handler returning a canned response."""

    def _handle(user_message: str, history=None) -> AgentResponse:  # noqa: ANN001
        resp: AgentResponse = AgentResponse(
            text=text,
            tools_used=list(tools) if tools is not None else ["bedrock"],
            time_ms=5,
        )
        if structured is not None:
            resp["structured_data"] = structured
        if chain_next is not None:
            resp["chain_next"] = chain_next
        return resp

    return _handle


# --------------------------------------------------------------------------- #
# 3.1 Routing preservation: agent + the four exact fallback reasons
# --------------------------------------------------------------------------- #

# The registry snapshot the router reasons over. Only these names are eligible.
_KNOWN_AGENTS = [
    "license_advisor",
    "application_intake",
    "compliance_screener",
    "crm_agent",
    "appointment_scheduler",
]


def _routing_registry() -> dict:
    return _agent_registry({name: _license_handler("ok") for name in _KNOWN_AGENTS})


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    chosen=st.sampled_from(_KNOWN_AGENTS),
    reason=st.text(min_size=0, max_size=400),
    message=st.text(min_size=1, max_size=80),
)
def test_routing_confident_selection_preserved(chosen, reason, message):
    """A valid model selection routes to that agent with the (bounded) reason (3.1)."""
    orch = Orchestrator(
        registry=_routing_registry(),
        bedrock=_FakeBedrockSelecting(chosen, reason),
        default_agent="license_advisor",
    )
    decision = orch.route(message, [])
    assert isinstance(decision, RoutingDecision)
    assert decision.agent == chosen
    # Reason is bounded to MAX_REASON_CHARS and stripped.
    assert decision.routing_reason == reason.strip()[:MAX_REASON_CHARS]
    assert len(decision.routing_reason) <= MAX_REASON_CHARS


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    unknown=st.text(min_size=1, max_size=20).filter(
        lambda s: s.strip() and s.strip().lower() != "none" and s.strip() not in _KNOWN_AGENTS
    ),
    message=st.text(min_size=1, max_size=80),
)
def test_routing_unknown_agent_falls_back(unknown, message):
    """An agent name absent from the registry falls back with the exact reason (3.1)."""
    orch = Orchestrator(
        registry=_routing_registry(),
        bedrock=_FakeBedrockSelecting(unknown),
        default_agent="license_advisor",
    )
    decision = orch.route(message, [])
    assert decision.agent == "license_advisor"
    assert decision.routing_reason == REASON_UNKNOWN_NAME


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(message=st.text(min_size=1, max_size=80), no_match=st.sampled_from(["none", "None", "NONE", ""]))
def test_routing_no_confident_match_falls_back(message, no_match):
    """The 'none'/empty sentinel falls back with the no-confident-match reason (3.1)."""
    orch = Orchestrator(
        registry=_routing_registry(),
        bedrock=_FakeBedrockSelecting(no_match),
        default_agent="license_advisor",
    )
    decision = orch.route(message, [])
    assert decision.agent == "license_advisor"
    assert decision.routing_reason == REASON_NO_MATCH


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    raw=st.text(min_size=0, max_size=60).filter(lambda s: not _is_parseable_selection(s)),
    message=st.text(min_size=1, max_size=80),
)
def test_routing_malformed_output_falls_back(raw, message):
    """Non-JSON / wrong-shape model output falls back with the malformed reason (3.1)."""
    orch = Orchestrator(
        registry=_routing_registry(),
        bedrock=_FakeBedrockRaw(raw),
        default_agent="license_advisor",
    )
    decision = orch.route(message, [])
    assert decision.agent == "license_advisor"
    assert decision.routing_reason == REASON_MALFORMED


def _is_parseable_selection(raw: str) -> bool:
    """True if `raw` would parse into a usable {"agent": <str>} selection."""
    text = raw.strip()
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(parsed, dict) and isinstance(parsed.get("agent"), str)


def test_routing_bedrock_error_falls_back_malformed():
    """A Bedrock/network error routes to default with the malformed reason (3.1)."""
    orch = Orchestrator(
        registry=_routing_registry(),
        bedrock=_FakeBedrockRaising(),
        default_agent="license_advisor",
    )
    decision = orch.route("anything", [])
    assert decision.agent == "license_advisor"
    assert decision.routing_reason == REASON_MALFORMED


def test_routing_timeout_reason_string_preserved():
    """The four fallback reason strings are exactly as specified (3.1)."""
    assert REASON_TIMEOUT == "routing timed out; using default agent"
    assert REASON_MALFORMED == "malformed selection output; using default agent"
    assert REASON_UNKNOWN_NAME == "unknown agent name; using default agent"
    assert REASON_NO_MATCH == "no confident match; using default agent"


# --------------------------------------------------------------------------- #
# 3.4 Trace_Entry shape (agent_selected, routing_reason, tools_used, time_ms)
# --------------------------------------------------------------------------- #


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    agent=st.sampled_from(_KNOWN_AGENTS),
    reason=st.text(min_size=0, max_size=60),
    tools=st.lists(st.sampled_from(["bedrock", "dynamodb", "fallback-mode"]), max_size=3),
    time_ms=st.integers(min_value=0, max_value=100000),
)
def test_trace_entry_shape_preserved(agent, reason, tools, time_ms):
    """Every Trace_Entry keeps its four fields with the right types (3.4)."""
    response = {"text": "hi", "tools_used": tools, "time_ms": time_ms}
    entry = runtime.build_trace_entry(agent, response, routing_reason=reason)
    assert entry["agent_selected"] == agent
    assert entry["routing_reason"] == reason
    assert isinstance(entry["tools_used"], list)
    assert entry["tools_used"] == tools
    assert isinstance(entry["time_ms"], int) and not isinstance(entry["time_ms"], bool)
    assert entry["time_ms"] >= 0
    assert entry["time_ms"] == time_ms


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    bad_tools=st.sampled_from([None, "bedrock", 5, {"x": 1}]),
    bad_time=st.sampled_from([-1, "x", None, 1.5]),
)
def test_trace_entry_normalises_bad_values(bad_tools, bad_time):
    """Non-list tools_used and non-int/negative time_ms normalise to [] / 0 (3.4)."""
    response = {"text": "hi", "tools_used": bad_tools, "time_ms": bad_time}
    entry = runtime.build_trace_entry("license_advisor", response)
    assert entry["tools_used"] == []
    assert entry["time_ms"] == 0


# --------------------------------------------------------------------------- #
# 3.2 Contract rejection (no text, no Trace_Entry)  +  3.6 history accounting
# --------------------------------------------------------------------------- #


def _process(handlers, chosen_agent, message, *, store=None, reason="ok"):
    """Drive process_message with an injected orchestrator + registry.

    Patches the module-level AGENT_REGISTRY main/runtime read from so both the
    primary invocation and the CRM auto-fire resolve against our fakes.
    """
    registry = _agent_registry(handlers)
    store = store if store is not None else InMemorySessionStore()
    orch = Orchestrator(
        registry=registry, bedrock=_FakeBedrockSelecting(chosen_agent, reason),
        default_agent="license_advisor",
    )
    events: list[dict] = []

    import agents as agents_pkg

    original = dict(agents_pkg.AGENT_REGISTRY)
    agents_pkg.AGENT_REGISTRY.clear()
    agents_pkg.AGENT_REGISTRY.update(registry)
    try:
        result = main.process_message(
            "session-1", message, events.append, orchestrator=orch, store=store
        )
    finally:
        agents_pkg.AGENT_REGISTRY.clear()
        agents_pkg.AGENT_REGISTRY.update(original)
    return result, events, store


@settings(max_examples=120, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(message=st.text(min_size=1, max_size=60).filter(lambda s: s.strip()))
def test_contract_violation_no_text_no_trace(message):
    """A contract-violating primary response yields no text and no Trace_Entry (3.2)."""
    # Empty text violates the contract (text must be non-empty).
    bad = lambda m, h=None: {"text": "", "tools_used": ["bedrock"], "time_ms": 1}  # noqa: E731
    handlers = {"application_intake": bad}
    result, events, store = _process(handlers, "application_intake", message)

    assert result["message"] == ""
    assert result["trace"] == []
    trace_events = [e for e in events if e.get("type") == "trace"]
    assert trace_events == []
    # No assistant text -> nothing appended to history (3.6 corollary).
    assert store.get_history("session-1") == []


@settings(max_examples=120, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(message=st.text(min_size=1, max_size=60).filter(lambda s: s.strip()))
def test_history_one_user_one_assistant_turn(message):
    """A reply appends exactly one user turn then one assistant turn (3.6)."""
    handlers = {"application_intake": _license_handler("Here is your draft.")}
    result, events, store = _process(handlers, "application_intake", message)

    history = store.get_history("session-1")
    assert len(history) == 2
    assert history[0] == {"role": "user", "content": message}
    assert history[1] == {"role": "assistant", "content": result["message"]}


# --------------------------------------------------------------------------- #
# 3.8 Recommendation flow: two entries + one NEW LEAD-<timestamp> lead
# 3.9 Non-License_Advisor routing: no auto-fire
# --------------------------------------------------------------------------- #


def _crm_handler(table):
    """A crm_agent handler using the real crm handle with an injected table."""
    import agents.crm_agent as crm

    def _handle(user_message: str, history=None) -> AgentResponse:  # noqa: ANN001
        return crm.handle(user_message, history, table=table)

    return _handle


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(message=st.text(min_size=1, max_size=60).filter(lambda s: s.strip()))
def test_recommendation_flow_two_entries_one_lead(message):
    """License_Advisor + auto-fired CRM = two entries and one NEW LEAD lead (3.8)."""
    table = _FakeTable()
    handlers = {
        "license_advisor": _license_handler(
            "The Startup package at AED 22,000/year fits your needs."
        ),
        "crm_agent": _crm_handler(table),
    }
    result, events, store = _process(handlers, "license_advisor", message)

    trace = result["trace"]
    assert len(trace) == 2
    assert trace[0]["agent_selected"] == "license_advisor"
    assert trace[1]["agent_selected"] == "crm_agent"

    # Exactly one NEW lead written with the required fields (3.8).
    assert len(table.items) == 1
    lead = table.items[0]
    assert lead["lead_id"].startswith("LEAD-")
    assert lead["source"] == "aws-summit-demo"
    assert lead["status"] == "NEW"
    assert lead["interaction_summary"]
    assert lead["created_at"].endswith("Z")


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    agent=st.sampled_from(
        ["application_intake", "compliance_screener", "crm_agent", "appointment_scheduler"]
    ),
    message=st.text(min_size=1, max_size=60).filter(lambda s: s.strip()),
)
def test_non_license_advisor_no_auto_fire(agent, message):
    """A message routed to a non-License_Advisor agent does not auto-fire CRM (3.9)."""
    table = _FakeTable()
    handlers = {
        agent: _license_handler("Primary agent handled it.")
        if agent != "crm_agent"
        else _crm_handler(table),
        # A distinct crm_agent present but must not be auto-fired for non-LA routes.
        "crm_agent": _crm_handler(table),
    }
    result, events, store = _process(handlers, agent, message)

    crm_entries = [e for e in result["trace"] if e["agent_selected"] == "crm_agent"]
    if agent == "crm_agent":
        # Direct route to crm runs it as primary exactly once; no *auto-fire*.
        assert len(crm_entries) == 1
        assert len(table.items) == 1
    else:
        assert crm_entries == []
        assert table.items == []


# --------------------------------------------------------------------------- #
# 3.7 Chaining behavior
# --------------------------------------------------------------------------- #


@settings(max_examples=80, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(message=st.text(min_size=1, max_size=40).filter(lambda s: s.strip()))
def test_chaining_invokes_named_agent_and_emits_trace(message):
    """chain_next invokes the named agent in order, appending text + one trace (3.7)."""
    handlers = {
        "compliance_screener": _license_handler("Screening result.", tools=["bedrock"]),
        "appointment_scheduler": _license_handler("Slot booked.", tools=["bedrock"]),
    }
    reg = _agent_registry(handlers)
    events: list[dict] = []
    steps = runtime.run_chain(
        "appointment_scheduler", message, [], events.append, registry=reg
    )
    assert len(steps) == 1
    assert steps[0]["text"] == "Slot booked."
    assert len(events) == 1
    assert events[0]["agent_selected"] == "appointment_scheduler"


def test_chaining_stops_on_missing_agent():
    """A chain_next naming an absent agent is skipped without failing (3.7)."""
    reg = _agent_registry({"compliance_screener": _license_handler("x")})
    events: list[dict] = []
    steps = runtime.run_chain("no_such_agent", "hi", [], events.append, registry=reg)
    assert steps == []
    assert events == []


def test_chaining_caps_and_avoids_repeats():
    """A repeated agent stops the chain; length capped at MAX_CHAIN_STEPS (3.7)."""

    def _a(m, h=None):
        return AgentResponse(text="A", tools_used=["bedrock"], time_ms=1, chain_next="b")

    def _b(m, h=None):
        return AgentResponse(text="B", tools_used=["bedrock"], time_ms=1, chain_next="a")

    reg = _agent_registry({"a": _a, "b": _b})
    events: list[dict] = []
    steps = runtime.run_chain("a", "hi", [], events.append, registry=reg)
    # a -> b -> (a already seen, stops). Two distinct steps.
    assert [s["text"] for s in steps] == ["A", "B"]
    assert len(events) == 2


def test_chaining_rejects_contract_violation():
    """A contract-violating chained step is dropped with no trace (3.2/3.7)."""
    bad = lambda m, h=None: {"text": "", "tools_used": [], "time_ms": 1}  # noqa: E731
    reg = _agent_registry({"bad_agent": bad})
    events: list[dict] = []
    steps = runtime.run_chain("bad_agent", "hi", [], events.append, registry=reg)
    assert steps == []
    assert events == []


# --------------------------------------------------------------------------- #
# 3.3 Degradation to Fallback_Response with tools_used ["fallback-mode"]
# --------------------------------------------------------------------------- #


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(message=st.text(min_size=1, max_size=60))
def test_invoke_agent_degrades_to_fallback(message):
    """A Bedrock error degrades to the fallback text with ["fallback-mode"] (3.3)."""

    class _Raising:
        def invoke_model(self, *, modelId, body):  # noqa: N803, ANN001
            raise RuntimeError("bedrock down")

    resp = bedrock_client.invoke_agent(
        system_prompt="sys",
        user_message=message,
        model_id="haiku",
        tools_used=["bedrock"],
        fallback_text="Sorry, I'm in fallback mode.",
        bedrock_client=_Raising(),
    )
    assert resp["text"] == "Sorry, I'm in fallback mode."
    assert resp["tools_used"] == ["fallback-mode"]
    assert isinstance(resp["time_ms"], int) and resp["time_ms"] >= 0


def test_crm_degrades_to_fallback_on_write_failure():
    """A DynamoDB write failure degrades CRM to fallback-mode (3.3)."""
    import agents.crm_agent as crm

    class _FailingTable:
        def put_item(self, *, Item):  # noqa: N803, ANN001
            raise RuntimeError("dynamo down")

    resp = crm.handle("interested in a package", None, table=_FailingTable())
    assert resp["tools_used"] == ["fallback-mode"]
    assert resp["text"]  # non-empty fallback text


# --------------------------------------------------------------------------- #
# 3.12 Per-agent user-visible rules (License_Advisor package/cost/149-word cap)
# --------------------------------------------------------------------------- #


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(package=st.sampled_from(["Idea", "Seed", "Startup", "Growth"]))
def test_license_advisor_detects_package_and_cost(package, monkeypatch):
    """A reply naming one package attaches structured_data with its AED cost (3.12)."""
    import agents.license_advisor as la

    cost = la.PACKAGES[package]["annual_cost_aed"]
    text = f"I recommend the {package} package at AED {cost:,}/year for you."

    def _fake_invoke(**kwargs):
        return AgentResponse(text=text, tools_used=["bedrock"], time_ms=3)

    monkeypatch.setattr(la, "invoke_agent", _fake_invoke)
    resp = la.handle("set up a company", [])
    assert resp["structured_data"]["recommended_package"] == package
    assert resp["structured_data"]["annual_cost_aed"] == cost


def test_license_advisor_custom_quote_no_package(monkeypatch):
    """A custom-quote reply attaches no fixed-package structured_data (3.12)."""
    import agents.license_advisor as la

    text = "Your needs exceed Growth, so we'll prepare a custom quote for you."

    monkeypatch.setattr(
        la, "invoke_agent",
        lambda **k: AgentResponse(text=text, tools_used=["bedrock"], time_ms=3),
    )
    resp = la.handle("I need 50 visas", [])
    assert "structured_data" not in resp


def test_license_advisor_caps_at_149_words(monkeypatch):
    """A recommendation reply is capped at 149 words (3.12)."""
    import agents.license_advisor as la

    long_text = "word " * 300  # 300 words
    monkeypatch.setattr(
        la, "invoke_agent",
        lambda **k: AgentResponse(text=long_text, tools_used=["bedrock"], time_ms=3),
    )
    resp = la.handle("recommend something", [])
    assert len(resp["text"].split()) <= la.MAX_WORDS


def test_license_advisor_fallback_untouched(monkeypatch):
    """A fallback response keeps ["fallback-mode"] and gets no structured_data (3.3/3.12)."""
    import agents.license_advisor as la

    monkeypatch.setattr(
        la, "invoke_agent",
        lambda **k: AgentResponse(
            text="Fallback text.", tools_used=["fallback-mode"], time_ms=3
        ),
    )
    resp = la.handle("Startup package", [])
    assert resp["tools_used"] == ["fallback-mode"]
    assert "structured_data" not in resp


# --------------------------------------------------------------------------- #
# 3.13 Credential source (SSO profile locally, default chain deployed)
# --------------------------------------------------------------------------- #


@settings(max_examples=60, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(profile=st.text(min_size=1, max_size=20).filter(lambda s: s.strip()))
def test_bedrock_client_uses_sso_profile_when_set(profile, monkeypatch):
    """With an SSO profile set, the Bedrock client is built from a named Session (3.13)."""
    import types

    calls = {"session_profile": None, "session_client": None, "top_client": None}

    class _FakeSession:
        def __init__(self, *, profile_name):
            calls["session_profile"] = profile_name

        def client(self, service, *, region_name=None):
            calls["session_client"] = service
            return object()

    fake_boto3 = types.SimpleNamespace(
        Session=_FakeSession,
        client=lambda service, region_name=None: calls.__setitem__("top_client", service),
    )
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)
    monkeypatch.setattr(bedrock_client.config, "aws_sso_profile", profile)

    bedrock_client._default_bedrock_client()
    assert calls["session_profile"] == profile
    assert calls["session_client"] == "bedrock-runtime"
    assert calls["top_client"] is None  # not the default chain


def test_bedrock_client_uses_default_chain_when_no_profile(monkeypatch):
    """With no SSO profile, the Bedrock client uses the default credential chain (3.13)."""
    import types

    calls = {"top_client": None, "session_used": False}

    class _FakeSession:
        def __init__(self, *, profile_name):
            calls["session_used"] = True

        def client(self, service, *, region_name=None):
            return object()

    fake_boto3 = types.SimpleNamespace(
        Session=_FakeSession,
        client=lambda service, region_name=None: calls.__setitem__("top_client", service) or object(),
    )
    monkeypatch.setitem(__import__("sys").modules, "boto3", fake_boto3)
    monkeypatch.setattr(bedrock_client.config, "aws_sso_profile", None)

    bedrock_client._default_bedrock_client()
    assert calls["top_client"] == "bedrock-runtime"
    assert calls["session_used"] is False
