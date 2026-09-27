"""Cross-cutting agent-handler contract + registry-extensibility tests (Task 19.1).

Two guards live here, both of which protect the *whole* system rather than a
single agent, which is why they sit in the shared test suite:

1. **The shared handler-contract test** (R3.3, R3.7, R2.2). A parametrized test
   iterates over *every* agent currently in ``AGENT_REGISTRY`` and asserts that
   invoking its ``handle(message, history)`` returns a value satisfying the
   ``AgentResponse`` contract — ``bedrock_client.validate_response`` returns
   ``None`` *and* the required fields have the correct types. This is the guard
   for teammates' future agents: any agent dropped into the registry is held to
   the same contract automatically, with no per-agent test needed.

2. **The registry-extensibility test** (R2.4). A throwaway "dummy" agent is
   registered *only within the test* via ``agents.register(...)`` and the
   Orchestrator is asked to route to it. The Orchestrator (built with a fake
   Bedrock that selects ``dummy_agent``) routes to the new agent with **no
   change to ``orchestrator.py``**, proving zero-touch extensibility. The
   registry entry is removed afterwards so other tests are unaffected.

All AWS boundaries (Bedrock, DynamoDB, S3) are mocked/injected so no live AWS
credentials or network access are required. The watchlist and calendar reads
use the local ``mock_data/*.json`` files (no AWS).
"""

from __future__ import annotations

import json

import pytest

import agents
from agents import AGENT_REGISTRY
import bedrock_client
from bedrock_client import AgentResponse, validate_response
from orchestrator import Orchestrator, RoutingDecision


# --------------------------------------------------------------------------- #
# Representative messages per agent
# --------------------------------------------------------------------------- #
#
# Each agent parses/branches on the message content. A single generic string
# would exercise only some paths, so we provide a representative message that
# drives each agent down a *valid* AgentResponse path (a completed action or a
# clarifying question — both are valid responses per the contract). Agents not
# listed here fall back to a sensible default message.
_REPRESENTATIVE_MESSAGES: dict[str, str] = {
    "license_advisor": "I want to set up a software company with 3 people, launching next month.",
    "application_intake": "I'd like to start a new business setup application.",
    # A name + identifier so the screener runs a full screening (not a
    # missing-input request). "John Doe" / "P1234567" match the mock watchlist.
    "compliance_screener": "Please screen John Doe with passport P1234567.",
    "crm_agent": "Interested in the Startup package for three visas.",
    "appointment_scheduler": "I'd like to book a meeting with a relationship manager.",
}

_DEFAULT_MESSAGE = "Hello, I need help with my business setup."


# --------------------------------------------------------------------------- #
# AWS-boundary mocks
# --------------------------------------------------------------------------- #


class _FakeTable:
    """A stand-in DynamoDB Table that records ``put_item`` calls in memory.

    Used so agents that persist (crm_agent, application_intake) exercise their
    real success path without touching AWS.
    """

    def __init__(self) -> None:
        self.items: list[dict] = []

    def put_item(self, *, Item: dict) -> dict:  # noqa: N803 - boto3 kwarg name
        self.items.append(Item)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _fake_invoke_agent(**kwargs) -> AgentResponse:
    """Stand-in for ``bedrock_client.invoke_agent`` returning a valid response.

    Preserves the caller's ``tools_used`` (e.g. ``["bedrock"]``) so the
    license_advisor success path is exercised without a live Bedrock call.
    """
    tools_used = list(kwargs.get("tools_used", ["bedrock"]))
    return AgentResponse(
        text="The Startup package at AED 22,000/year fits three visas and a fast launch.",
        tools_used=tools_used,
        time_ms=5,
    )


@pytest.fixture(autouse=True)
def _mock_aws(monkeypatch):
    """Mock every AWS boundary the in-scope agents touch (Bedrock + DynamoDB).

    * ``bedrock_client.invoke_agent`` -> canned valid response (license_advisor).
    * ``compliance_screener._parse_with_bedrock`` -> return parsed name/id from
      the message so the screener runs its deterministic, local watchlist match
      (no Bedrock call). The watchlist is read from local JSON.
    * ``crm_agent._default_table`` / ``application_intake._default_dynamodb_table``
      -> a :class:`_FakeTable` so the persist path never touches AWS.
    * ``appointment_scheduler`` reads the local calendar JSON (no AWS).
    """
    # License Advisor uses the shared helper; patch it to a valid response.
    monkeypatch.setattr(bedrock_client, "invoke_agent", _fake_invoke_agent)

    # Compliance Screener: parse name/identifier without calling Bedrock.
    import agents.compliance_screener as screener

    def _fake_parse(user_message, bedrock_client_arg):  # noqa: ANN001
        # Mirror the mock watchlist so the "flagged" path is exercised.
        if "john doe" in user_message.lower():
            return "John Doe", "P1234567"
        # Generic parse: let heuristics handle other messages.
        return None, None

    monkeypatch.setattr(screener, "_parse_with_bedrock", _fake_parse)

    # Persisting agents: inject an in-memory fake table via their default hooks.
    import agents.crm_agent as crm
    import agents.application_intake as intake

    fake_table = _FakeTable()
    monkeypatch.setattr(crm, "_default_table", lambda: fake_table)
    monkeypatch.setattr(intake, "_default_dynamodb_table", lambda: fake_table)

    return fake_table


# --------------------------------------------------------------------------- #
# 1. Shared agent-handler contract test (R3.3, R3.7, R2.2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("agent_name", sorted(AGENT_REGISTRY.keys()))
def test_registered_agent_handle_satisfies_contract(agent_name):
    """Every registered agent's ``handle`` returns a valid ``AgentResponse``.

    This is the guard for teammates' future agents (R2.2, R3.3, R3.7): any agent
    present in ``AGENT_REGISTRY`` is invoked uniformly with ``(message, history)``
    and its response must pass ``validate_response`` and carry the correctly
    typed required fields. A clarifying-question response is still a valid
    AgentResponse, so agents that ask instead of act also pass.
    """
    record = AGENT_REGISTRY[agent_name]

    # R2.1: the registry entry is well-formed (description + callable handler).
    assert "description" in record and isinstance(record["description"], str)
    assert callable(record["handler"]), f"{agent_name} handler is not callable"

    handler = record["handler"]
    message = _REPRESENTATIVE_MESSAGES.get(agent_name, _DEFAULT_MESSAGE)

    # Invoke uniformly: message + empty history. Every in-scope handler accepts
    # (user_message, history) positionally; AWS is mocked by the autouse fixture.
    response = handler(message, [])

    # R3.7 / R3.3: the shared guard accepts the response (no contract violation).
    violation = validate_response(response)
    assert violation is None, f"{agent_name} violated the contract: {violation}"

    # R3.3 / R3.4: required fields exist with the correct types (belt-and-braces
    # on top of validate_response so a regression in the guard can't hide one).
    assert isinstance(response["text"], str) and response["text"].strip() != ""
    assert isinstance(response["tools_used"], list)
    assert all(isinstance(tool, str) for tool in response["tools_used"])
    assert isinstance(response["time_ms"], int) and not isinstance(response["time_ms"], bool)
    assert response["time_ms"] >= 0

    # Optional fields, when present, keep their contract types.
    if "structured_data" in response:
        assert isinstance(response["structured_data"], dict)
    if "chain_next" in response:
        assert isinstance(response["chain_next"], str) and response["chain_next"].strip() != ""


def test_registry_is_non_empty():
    """Sanity guard: the five in-scope agents auto-registered on import (R2.2).

    Protects the parametrized test above from silently passing with zero cases
    if registration ever breaks.
    """
    assert set(AGENT_REGISTRY).issuperset(
        {
            "license_advisor",
            "application_intake",
            "compliance_screener",
            "crm_agent",
            "appointment_scheduler",
        }
    )


# --------------------------------------------------------------------------- #
# 2. Registry-extensibility test (R2.4)
# --------------------------------------------------------------------------- #


class _FakeBedrockSelecting:
    """A fake Bedrock Runtime client whose routing choice is fixed.

    Returns an ``invoke_model`` response whose assistant text is the strict JSON
    ``{"agent": <chosen>, "reason": ...}`` the Orchestrator expects, in the
    Anthropic messages response envelope the runtime parses.
    """

    def __init__(self, chosen_agent: str) -> None:
        self._chosen = chosen_agent

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        selection = json.dumps(
            {"agent": self._chosen, "reason": "dummy agent best matches the request"}
        )
        payload = {"content": [{"type": "text", "text": selection}]}
        return {"body": json.dumps(payload)}


def _dummy_handle(user_message: str, history=None) -> AgentResponse:  # noqa: ANN001
    """A throwaway agent handler used only to prove routability (R2.4)."""
    return AgentResponse(
        text="Dummy agent handled the request.",
        tools_used=["bedrock"],
        time_ms=1,
    )


def test_new_agent_is_routable_without_orchestrator_changes():
    """Registering a new agent makes it routable with no Orchestrator edits (R2.4).

    A dummy agent is registered into the live ``AGENT_REGISTRY`` at test time.
    An Orchestrator built with a fake Bedrock that selects ``dummy_agent`` then
    routes to it. Because the Orchestrator reads the registry at decision time
    and builds its routing prompt purely from registry descriptions, the new
    agent is selectable without touching ``orchestrator.py``. The dummy entry is
    removed afterwards so the shared registry is left as it was.
    """
    dummy_name = "dummy_agent"
    assert dummy_name not in AGENT_REGISTRY  # precondition: name is free

    try:
        # (1) Register a brand-new agent — the only step a teammate must do.
        agents.register(dummy_name, "A dummy test agent for extensibility.", _dummy_handle)
        assert dummy_name in AGENT_REGISTRY, "dummy agent failed to register"

        # (2) Build the *unchanged* Orchestrator with a fake Bedrock selecting it.
        orchestrator = Orchestrator(
            registry=AGENT_REGISTRY,
            bedrock=_FakeBedrockSelecting(dummy_name),
            default_agent="license_advisor",
        )

        # (3) Routing resolves to the new agent — no orchestrator.py change made.
        decision = orchestrator.route("route me to the dummy please", [])
        assert isinstance(decision, RoutingDecision)
        assert decision.agent == dummy_name, (
            f"expected route to '{dummy_name}', got '{decision.agent}' "
            f"(reason: {decision.routing_reason!r})"
        )

        # And the newly registered handler is invocable and honours the contract.
        response = AGENT_REGISTRY[dummy_name]["handler"]("hello", [])
        assert validate_response(response) is None
    finally:
        # Clean up so other tests see the registry unchanged.
        AGENT_REGISTRY.pop(dummy_name, None)

    assert dummy_name not in AGENT_REGISTRY  # postcondition: registry restored


def test_duplicate_registration_is_rejected_and_first_kept():
    """A duplicate dummy registration is rejected, keeping the first (R2.7).

    Complements the extensibility test: registering the same name twice must not
    clobber the first record, so a teammate's agent can't be silently overwritten.
    """
    dummy_name = "dummy_agent_dup"
    assert dummy_name not in AGENT_REGISTRY

    def _first(user_message, history=None):  # noqa: ANN001
        return AgentResponse(text="first", tools_used=["bedrock"], time_ms=1)

    def _second(user_message, history=None):  # noqa: ANN001
        return AgentResponse(text="second", tools_used=["bedrock"], time_ms=1)

    try:
        agents.register(dummy_name, "First dummy agent.", _first)
        agents.register(dummy_name, "Second dummy agent.", _second)
        # First record is retained (R2.7).
        assert AGENT_REGISTRY[dummy_name]["handler"] is _first
        assert AGENT_REGISTRY[dummy_name]["description"] == "First dummy agent."
    finally:
        AGENT_REGISTRY.pop(dummy_name, None)
