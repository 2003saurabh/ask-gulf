"""Bug-condition exploration tests for the context-aware-routing bugfix.

# Feature: context-aware-routing, Property 1: Bug Condition

Property 1 (design.md): for any input where the bug condition holds — the
previous assistant turn was an agent collecting information, the current user
turn is the in-context answer, and the user did not clearly change topic — the
fixed ``route()`` SHALL make the recent history and the last-selected agent
available to the routing decision and route the turn to the agent that was
collecting information.

These tests MUST FAIL on the UNFIXED code (routing ignores history and never
sees the last agent) and PASS after the fix threads recent history + last_agent
into the routing prompt.

Determinism: all AWS is mocked. The fake Bedrock/Sonnet inspects the routing
prompt (the ``system`` field of the invoke body) it is handed and returns a
decision tied to the fix's observable contract:
  * if the prompt does NOT carry the recent history AND the last agent (the
    UNFIXED behavior) it returns the stateless *misroute* — modelling how the
    real model re-classifies an isolated fragment;
  * if the prompt DOES carry them (the FIXED behavior) it returns the
    *in-progress* agent that was collecting information.
No real-model stochasticity is involved.

Run with ``.\\.venv\\Scripts\\python.exe -m pytest`` from ``backend/``.
"""

from __future__ import annotations

import json

from orchestrator import Orchestrator, RoutingDecision


# --------------------------------------------------------------------------- #
# Fake Sonnet that decides from the routing prompt it is given
# --------------------------------------------------------------------------- #


class _FakeContextAwareSonnet:
    """Fake Bedrock client whose decision depends on the routing prompt.

    It parses the invoke ``body``, reads the ``system`` prompt, and checks
    whether BOTH the recent-history marker text and the last-agent name appear.
    When both are present (FIXED router), it returns ``in_progress_agent``; when
    they are absent (UNFIXED router), it returns ``misroute_agent`` — modelling
    the real model's isolated re-classification of a bare follow-up.

    It records the last system prompt it saw so a test can assert the fix's
    observable contract: the prompt actually INCLUDES the recent history and the
    last agent.
    """

    def __init__(
        self,
        *,
        in_progress_agent: str,
        misroute_agent: str,
        history_markers: list[str],
        last_agent: str,
    ) -> None:
        self._in_progress = in_progress_agent
        self._misroute = misroute_agent
        self._history_markers = history_markers
        self._last_agent = last_agent
        self.last_system_prompt: str | None = None

    def prompt_has_history(self) -> bool:
        """True if the last system prompt carried the recent history turns."""
        sp = self.last_system_prompt or ""
        return all(marker in sp for marker in self._history_markers)

    def prompt_has_last_agent(self) -> bool:
        """True if the last system prompt named the last-selected agent."""
        return self._last_agent in (self.last_system_prompt or "")

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        parsed = json.loads(body)
        system_prompt = parsed.get("system", "")
        self.last_system_prompt = system_prompt

        if self.prompt_has_history() and self.prompt_has_last_agent():
            chosen = self._in_progress
            reason = "continuing the in-progress collection flow"
        else:
            chosen = self._misroute
            reason = "isolated fragment reads as this agent's intent"

        selection = json.dumps({"agent": chosen, "reason": reason})
        payload = {"content": [{"type": "text", "text": selection}]}
        return {"body": json.dumps(payload)}


class _FakeTopicChangeSonnet:
    """Fake that always returns the new-topic agent (models a clear topic change).

    Used for the 2.4 case: even mid-flow, a clear topic change routes to the
    agent matching the new topic rather than being forced to stay.
    """

    def __init__(self, *, new_topic_agent: str) -> None:
        self._new_topic = new_topic_agent

    def invoke_model(self, *, modelId: str, body: str) -> dict:  # noqa: N803
        selection = json.dumps(
            {"agent": self._new_topic, "reason": "user clearly changed topic"}
        )
        payload = {"content": [{"type": "text", "text": selection}]}
        return {"body": json.dumps(payload)}


_KNOWN_AGENTS = [
    "license_advisor",
    "application_intake",
    "compliance_screener",
    "crm_agent",
    "appointment_scheduler",
]


def _handler(text: str):
    def _handle(user_message: str, history=None):  # noqa: ANN001
        return {"text": text, "tools_used": ["bedrock"], "time_ms": 1}

    return _handle


def _registry() -> dict:
    return {
        name: {"description": f"desc for {name}", "handler": _handler("ok")}
        for name in _KNOWN_AGENTS
    }


def _call_route(orch: Orchestrator, message: str, history, last_agent: str):
    """Call route with last_agent, tolerating the unfixed signature.

    On UNFIXED code ``route`` has no ``last_agent`` parameter; fall back to the
    two-arg call so the test still exercises the (buggy) stateless path and
    fails on the assertion rather than erroring on the signature.
    """
    try:
        return orch.route(message, history, last_agent=last_agent)
    except TypeError:
        return orch.route(message, history)


# --------------------------------------------------------------------------- #
# REPRO 1: License Advisor multi-turn — "in next month" must stay with LA
# --------------------------------------------------------------------------- #


def test_repro1_timeline_answer_stays_with_license_advisor():
    """After license_advisor asks for the timeline, "in next month" -> license_advisor.

    UNFIXED: routing ignores history and never sees last_agent, so the prompt
    lacks that context and the fake models the misroute to appointment_scheduler
    -> this assertion FAILS (confirms the bug).
    FIXED: the prompt carries the history + last agent, the fake returns
    license_advisor -> PASS.
    """
    history = [
        {"role": "user", "content": "I want to set up a fintech company"},
        {"role": "assistant", "content": "Great. What is your target timeline for the license?"},
    ]
    fake = _FakeContextAwareSonnet(
        in_progress_agent="license_advisor",
        misroute_agent="appointment_scheduler",
        history_markers=["target timeline for the license"],
        last_agent="license_advisor",
    )
    orch = Orchestrator(
        registry=_registry(), bedrock=fake, default_agent="license_advisor"
    )

    decision = _call_route(orch, "in next month", history, "license_advisor")

    assert isinstance(decision, RoutingDecision)
    # Observable contract of the fix: prompt carries recent history + last agent.
    assert fake.prompt_has_history(), "routing prompt must include the recent history"
    assert fake.prompt_has_last_agent(), "routing prompt must name the last-selected agent"
    # The in-progress flow is preserved.
    assert decision.agent == "license_advisor"


# --------------------------------------------------------------------------- #
# REPRO 2: Application Intake — "Gulf Tech LLC" must stay with application_intake
# --------------------------------------------------------------------------- #


def test_repro2_company_name_stays_with_application_intake():
    """After application_intake asks for the company name, "Gulf Tech LLC" -> application_intake.

    UNFIXED: misroute to compliance_screener -> FAILS. FIXED: stays with
    application_intake -> PASS.
    """
    history = [
        {"role": "user", "content": "I'd like to start my application"},
        {"role": "assistant", "content": "Sure. What is your company name?"},
    ]
    fake = _FakeContextAwareSonnet(
        in_progress_agent="application_intake",
        misroute_agent="compliance_screener",
        history_markers=["What is your company name?"],
        last_agent="application_intake",
    )
    orch = Orchestrator(
        registry=_registry(), bedrock=fake, default_agent="license_advisor"
    )

    decision = _call_route(orch, "Gulf Tech LLC", history, "application_intake")

    assert fake.prompt_has_history(), "routing prompt must include the recent history"
    assert fake.prompt_has_last_agent(), "routing prompt must name the last-selected agent"
    assert decision.agent == "application_intake"


# --------------------------------------------------------------------------- #
# 2.4: A clear topic change mid-flow follows the new topic, not the prior agent
# --------------------------------------------------------------------------- #


def test_topic_change_midflow_follows_new_topic():
    """Mid-flow, a clear topic change routes to the new-topic agent (2.4).

    Even though last_agent is license_advisor and a collection flow was in
    progress, a clear topic change is NOT forced to stay. This passes on both
    unfixed and fixed code; it guards against an over-eager "always stay" fix.
    """
    history = [
        {"role": "user", "content": "I want to set up a fintech company"},
        {"role": "assistant", "content": "What is your target timeline for the license?"},
    ]
    fake = _FakeTopicChangeSonnet(new_topic_agent="compliance_screener")
    orch = Orchestrator(
        registry=_registry(), bedrock=fake, default_agent="license_advisor"
    )

    decision = _call_route(
        orch,
        "Actually, what compliance rules apply to a fintech in the DIFC?",
        history,
        "license_advisor",
    )

    assert decision.agent == "compliance_screener"
