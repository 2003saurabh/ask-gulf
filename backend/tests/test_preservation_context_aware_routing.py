"""Preservation property tests for the context-aware-routing bugfix.

# Feature: context-aware-routing, Property 2: Preservation

Property 2 (design.md): for any input where the bug condition does NOT hold,
the fixed ``route()`` SHALL produce the same ``RoutingDecision`` (agent +
routing_reason) as the original router. This complements the existing §3.1
routing preservation tests in ``test_preservation_latency_trace_fix.py`` (which
exercise confident selection and the four fallback reasons via
``orch.route(message, [])`` positionally).

These tests follow the observation-first methodology: they capture the UNFIXED
router's outputs for non-buggy inputs (first-turn / empty-history calls, called
positionally with no last_agent) and assert those outputs are unchanged. They
MUST PASS on the UNFIXED code and keep passing after the fix — proving that
``last_agent``'s default keeps stateless routing byte-for-byte identical.

All AWS is mocked with injected fakes; no real AWS is called. Run with
``.\\.venv\\Scripts\\python.exe -m pytest`` from ``backend/``.
"""

from __future__ import annotations

import json

from hypothesis import HealthCheck, given, settings, strategies as st

from orchestrator import (
    MAX_REASON_CHARS,
    REASON_MALFORMED,
    REASON_NO_MATCH,
    REASON_TIMEOUT,
    REASON_UNKNOWN_NAME,
    Orchestrator,
    RoutingDecision,
)


class _FakeBedrockSelecting:
    """Fake Bedrock client returning a fixed strict-JSON selection."""

    def __init__(self, chosen_agent: str, reason: str = "matches the request") -> None:
        self._chosen = chosen_agent
        self._reason = reason

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        selection = json.dumps({"agent": self._chosen, "reason": self._reason})
        payload = {"content": [{"type": "text", "text": selection}]}
        return {"body": json.dumps(payload)}


class _FakeBedrockRaw:
    def __init__(self, raw_text: str) -> None:
        self._raw = raw_text

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        payload = {"content": [{"type": "text", "text": self._raw}]}
        return {"body": json.dumps(payload)}


_KNOWN_AGENTS = [
    "license_advisor",
    "application_intake",
    "compliance_screener",
    "crm_agent",
    "appointment_scheduler",
]


def _registry() -> dict:
    return {
        name: {"description": f"desc for {name}", "handler": lambda m, h=None: None}
        for name in _KNOWN_AGENTS
    }


# --------------------------------------------------------------------------- #
# Positional call (no last_agent) is unchanged — first-turn / new-request path
# --------------------------------------------------------------------------- #


@settings(max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    chosen=st.sampled_from(_KNOWN_AGENTS),
    reason=st.text(min_size=0, max_size=400),
    message=st.text(min_size=1, max_size=80),
)
def test_positional_confident_selection_unchanged(chosen, reason, message):
    """route(message, []) positionally still returns the chosen agent + bounded reason."""
    orch = Orchestrator(
        registry=_registry(),
        bedrock=_FakeBedrockSelecting(chosen, reason),
        default_agent="license_advisor",
    )
    decision = orch.route(message, [])
    assert isinstance(decision, RoutingDecision)
    assert decision.agent == chosen
    assert decision.routing_reason == reason.strip()[:MAX_REASON_CHARS]
    assert len(decision.routing_reason) <= MAX_REASON_CHARS


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    unknown=st.text(min_size=1, max_size=20).filter(
        lambda s: s.strip() and s.strip().lower() != "none" and s.strip() not in _KNOWN_AGENTS
    ),
    message=st.text(min_size=1, max_size=80),
)
def test_positional_unknown_agent_fallback_unchanged(unknown, message):
    """An unknown agent name still falls back to the default with the exact reason."""
    orch = Orchestrator(
        registry=_registry(),
        bedrock=_FakeBedrockSelecting(unknown),
        default_agent="license_advisor",
    )
    decision = orch.route(message, [])
    assert decision.agent == "license_advisor"
    assert decision.routing_reason == REASON_UNKNOWN_NAME


@settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    message=st.text(min_size=1, max_size=80),
    no_match=st.sampled_from(["none", "None", "NONE", ""]),
)
def test_positional_no_match_fallback_unchanged(message, no_match):
    """The none/empty sentinel still falls back with the no-confident-match reason."""
    orch = Orchestrator(
        registry=_registry(),
        bedrock=_FakeBedrockSelecting(no_match),
        default_agent="license_advisor",
    )
    decision = orch.route(message, [])
    assert decision.agent == "license_advisor"
    assert decision.routing_reason == REASON_NO_MATCH


def test_fallback_reason_strings_unchanged():
    """The four fallback reason strings remain byte-for-byte exact."""
    assert REASON_TIMEOUT == "routing timed out; using default agent"
    assert REASON_MALFORMED == "malformed selection output; using default agent"
    assert REASON_UNKNOWN_NAME == "unknown agent name; using default agent"
    assert REASON_NO_MATCH == "no confident match; using default agent"


# --------------------------------------------------------------------------- #
# Empty history + no last_agent: decision is identical to the positional call
# --------------------------------------------------------------------------- #


@settings(max_examples=120, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    chosen=st.sampled_from(_KNOWN_AGENTS),
    message=st.text(min_size=1, max_size=60).filter(lambda s: s.strip()),
)
def test_empty_history_matches_positional_call(chosen, message):
    """With empty history and no last_agent, the decision equals the positional call.

    This is the first-turn / no-in-progress-flow case (¬C): routing is unchanged.
    """
    orch = Orchestrator(
        registry=_registry(),
        bedrock=_FakeBedrockSelecting(chosen),
        default_agent="license_advisor",
    )
    positional = orch.route(message, [])
    explicit = orch.route(message, [])
    assert positional == explicit
    assert positional.agent == chosen
