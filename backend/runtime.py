"""Runtime post-processing: chaining engine and CRM auto-fire hook.

This module holds the two pieces of runtime orchestration that run *after* the
Orchestrator has selected and invoked a primary agent (design §5 "Chaining
Engine" and §6 "CRM Auto-Fire Hook"). It is deliberately separate from the
FastAPI surface (Task 14, ``backend/main.py``) so the WebSocket layer can import
clean, framework-agnostic functions:

* :func:`run_chain` — follows an agent response's ``chain_next`` flag,
  invoking each named agent in turn while it exists in the registry, appending
  its text, and emitting one ``Trace_Entry`` per step. Chain length is capped
  to prevent cycles, and a ``chain_next`` naming an absent agent is skipped so
  the message is never failed (R9.1, R9.2, R9.3, R9.4).
* :func:`crm_auto_fire` — the CRM auto-fire post-processing hook. When the
  Orchestrator-selected agent was ``license_advisor`` **and its reply was a
  genuine recommendation** (the primary response carries a truthy
  ``is_recommendation`` signal), this invokes ``crm_agent`` for the same message
  and emits its ``Trace_Entry``, yielding exactly two trace entries for the one
  message (R7.1, R7.5). A greeting / clarifying-question License Advisor turn is
  NOT a recommendation, so the hook is a no-op for it — no lead is written, no
  CRM ``Trace_Entry`` is produced, and no CRM text is added (defect C6). The CRM
  confirmation text is deliberately NOT merged into the user-facing reply; the
  lead capture surfaces only through the CRM ``Trace_Entry`` (defect C2).

Both accept an ``emit_trace`` callback so the WebSocket layer can stream each
``Trace_Entry`` live as it is produced (R11.2). Every agent response is guarded
with :func:`bedrock_client.validate_response` before its trace is built or its
text is appended, so a contract-violating response never corrupts the trace or
the running message (R3.7). The registry defaults to ``AGENT_REGISTRY`` from
``backend/agents`` but is injectable for tests.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from bedrock_client import validate_response

# The name of the primary agent whose completion triggers the CRM auto-fire
# hook, and the agent invoked by that hook (design §6, R7.1).
LICENSE_ADVISOR = "license_advisor"
CRM_AGENT = "crm_agent"

# Cap on chain length to prevent cycles (design §5, R9). At most this many
# distinct agents are invoked across a single chain.
MAX_CHAIN_STEPS = 5

# A Trace_Entry is a plain dict; a callback receiving each one as it is emitted.
TraceEntry = dict[str, Any]
EmitTrace = Callable[[TraceEntry], None]


def _registry() -> dict:
    """Return the live ``AGENT_REGISTRY`` from the agents package.

    Imported lazily so importing this module never forces agent registration
    (which imports boto3-touching modules) and so tests can inject their own
    registry instead.
    """
    from agents import AGENT_REGISTRY

    return AGENT_REGISTRY


def build_trace_entry(
    agent_name: str, response: dict, routing_reason: str = ""
) -> TraceEntry:
    """Build a ``Trace_Entry`` from an agent name and its ``AgentResponse``.

    A ``Trace_Entry`` carries ``agent_selected``, ``routing_reason``,
    ``tools_used`` (a list), and ``time_ms`` (a non-negative integer). The
    ``tools_used`` and ``time_ms`` values come straight from the agent's
    response (design §5, Data Models "Trace_Entry"); ``routing_reason`` is
    supplied by the caller (the Orchestrator's decision for the primary agent,
    or a chain/auto-fire reason for subsequent invocations).

    Args:
        agent_name: the registry name of the invoked agent.
        response: the agent's ``AgentResponse`` dict.
        routing_reason: the routing justification for this invocation.

    Returns:
        A well-formed ``Trace_Entry`` dict.
    """
    tools_used = response.get("tools_used", [])
    time_ms = response.get("time_ms", 0)
    return {
        "agent_selected": agent_name,
        "routing_reason": routing_reason,
        "tools_used": list(tools_used) if isinstance(tools_used, list) else [],
        "time_ms": time_ms if isinstance(time_ms, int) and time_ms >= 0 else 0,
    }


def _invoke(handler: Callable, message: str, history: Optional[list[dict]]) -> dict:
    """Invoke an agent handler with the uniform ``(message, history)`` args.

    Every in-scope handler accepts ``(user_message, history)`` positionally and
    any additional arguments are keyword-only with defaults, so a two-argument
    positional call works uniformly across all agents (design §3).
    """
    return handler(message, history)


def run_chain(
    first_agent: str,
    message: str,
    history: Optional[list[dict]],
    emit_trace: EmitTrace,
    registry: Optional[dict] = None,
) -> list[dict]:
    """Run the chaining engine starting from ``first_agent`` (design §5, R9).

    Follows each response's ``chain_next`` flag: while the named agent exists in
    the registry, it is invoked with the same ``message``/``history``, its text
    contributes to the running response, and one ``Trace_Entry`` is emitted per
    step (R9.1, R9.2). The loop is bounded three ways to prevent cycles: an
    agent already seen in this chain is not re-invoked, and the chain stops once
    :data:`MAX_CHAIN_STEPS` distinct agents have run (R9).

    Graceful degradation (R9.4): a ``chain_next`` naming an agent that is not in
    the registry (for example ``fee_calculator`` before a teammate adds it) is
    skipped — the chain simply stops there — and the message is never failed. A
    response that violates the agent contract is likewise skipped without
    emitting a trace or contributing text (R3.7), and the chain stops.

    Args:
        first_agent: the name of the agent that begins the chain. Usually the
            ``chain_next`` from the primary agent's response; if it is absent
            from the registry the returned list is empty (R9.4).
        message: the user message passed unchanged to every chained agent.
        history: prior conversation turns, passed unchanged to each agent.
        emit_trace: callback invoked with each ``Trace_Entry`` as it is produced
            so the WebSocket layer can stream it live (R11.2).
        registry: the agent registry to resolve handlers from; defaults to the
            live ``AGENT_REGISTRY``. Injectable for tests.

    Returns:
        The list of ``AgentResponse`` dicts produced by the chain, in order.
        Empty when ``first_agent`` is absent or produces no valid step.
    """
    reg = registry if registry is not None else _registry()

    steps: list[dict] = []
    seen: set[str] = set()
    current: Optional[str] = first_agent

    while current and current not in seen and len(seen) < MAX_CHAIN_STEPS:
        seen.add(current)

        rec = reg.get(current)
        if rec is None:
            # Chained agent not present in the registry -> skip, do not fail the
            # message; the chain simply ends here (R9.4).
            break

        response = _invoke(rec["handler"], message, history)

        # Guard the response against the contract before it affects the trace or
        # the running message (R3.7). A violating step is dropped and the chain
        # stops rather than emitting a malformed Trace_Entry.
        if validate_response(response) is not None:
            break

        emit_trace(
            build_trace_entry(
                current,
                response,
                routing_reason=f"chained from previous agent to {current}",
            )
        )
        steps.append(response)

        chain_next = response.get("chain_next")
        current = chain_next if isinstance(chain_next, str) and chain_next else None

    return steps


def _is_recommendation(primary_response: Optional[dict]) -> bool:
    """Return ``True`` when the primary response signals a genuine recommendation.

    Primary signal (task 3.4): the additive, non-user-visible
    ``is_recommendation`` flag the License Advisor sets when its reply recommends
    one package with its AED cost (R4.2, R4.3) or offers a custom quote (R4.5).

    Fallback signal: when that explicit flag is absent (for example a caller /
    test that produces a License Advisor reply without going through the flag-
    setting handler), the recommendation is derived directly from the reply text
    using the License Advisor's own detection — a named fixed package or a custom
    quote counts as a recommendation. This keeps the gate correct end-to-end for
    a real recommendation while still filtering out greetings / clarifying
    questions, which name no package and offer no quote.

    Greetings, clarifying questions, packages-mentioned-without-a-pick, and
    fallbacks leave both signals falsy, so this returns ``False`` for them. A
    missing response (contract-rejected primary) is never a recommendation.
    """
    if not isinstance(primary_response, dict):
        return False
    if primary_response.get("is_recommendation"):
        return True

    # Fallback: derive from the reply text via the License Advisor's own
    # recommendation detection. Imported lazily so importing this module never
    # forces the agents package (which touches boto3) at import time.
    text = primary_response.get("text")
    if not isinstance(text, str) or not text:
        return False
    try:
        from agents.license_advisor import _is_recommendation as _la_is_recommendation
    except Exception:  # noqa: BLE001 - detection is best-effort; absent module -> no fire
        return False
    return bool(_la_is_recommendation(text))


def crm_auto_fire(
    selected_agent: str,
    message: str,
    history: Optional[list[dict]],
    emit_trace: EmitTrace,
    registry: Optional[dict] = None,
    *,
    primary_response: Optional[dict] = None,
) -> Optional[dict]:
    """Run the CRM auto-fire post-processing hook (design §6, R7.1, R7.5).

    When the Orchestrator-selected agent was ``license_advisor`` **and its reply
    was a genuine recommendation** (``primary_response`` carries a truthy
    ``is_recommendation`` signal), the runtime invokes ``crm_agent`` for the same
    user message and emits its ``Trace_Entry``. Combined with the License
    Advisor's own trace, this yields exactly two trace entries for the one
    message (R7.5). The hook is distinct from chaining: it is not driven by a
    ``chain_next`` flag (design §6).

    The hook is a no-op (returns ``None`` and emits nothing) when the selected
    agent was not the License Advisor, when the License Advisor reply was NOT a
    recommendation (a greeting / clarifying question — defect C6), or when
    ``crm_agent`` is not registered, so it never fails the message. A CRM
    response that violates the contract is also dropped without emitting a trace
    (R3.7).

    The returned CRM ``AgentResponse`` is intended for the trace only — its
    confirmation text is deliberately NOT merged into the user-facing reply by
    the caller (defect C2). The lead capture surfaces solely through the CRM
    ``Trace_Entry`` emitted here.

    Args:
        selected_agent: the agent the Orchestrator selected for this message.
        message: the user message, passed unchanged to ``crm_agent``.
        history: prior conversation turns, passed unchanged to ``crm_agent``.
        emit_trace: callback invoked with the CRM ``Trace_Entry`` when produced,
            so the WebSocket layer can stream it live (R11.2).
        registry: the agent registry; defaults to the live ``AGENT_REGISTRY``.
            Injectable for tests.
        primary_response: the primary License Advisor ``AgentResponse``; the hook
            gates on its ``is_recommendation`` signal. When ``None`` or not a
            recommendation, the hook does not fire (defect C6).

    Returns:
        The ``crm_agent`` ``AgentResponse`` dict when the hook fired and
        produced a valid response, otherwise ``None``.
    """
    if selected_agent != LICENSE_ADVISOR:
        return None

    # Defect C6: gate on a genuine recommendation. A greeting / clarifying-
    # question License Advisor turn must not write a lead or emit a CRM trace.
    if not _is_recommendation(primary_response):
        return None

    reg = registry if registry is not None else _registry()

    rec = reg.get(CRM_AGENT)
    if rec is None:
        # CRM agent not registered -> nothing to auto-fire; never fail (R9.4).
        return None

    response = _invoke(rec["handler"], message, history)

    # Guard the CRM response; a violating response is dropped so it neither adds
    # a malformed Trace_Entry nor a bogus second trace (R3.7).
    if validate_response(response) is not None:
        return None

    emit_trace(
        build_trace_entry(
            CRM_AGENT,
            response,
            routing_reason="auto-fired after license_advisor completed",
        )
    )
    return response
