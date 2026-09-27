"""The Orchestrator router with fallback routing (design §1, R1).

The Orchestrator is a **pure router**: it receives every user message before
any specialist (R1.1), asks Bedrock Claude 3.5 Sonnet to reason over the
registered agents' descriptions, and selects exactly one specialist to handle
the message. It never generates an answer of its own (R1.9, R1.10) and it never
decides the agent with keyword matching or ``if/else`` branching — the choice
comes purely from the model reasoning over ``Agent_Description`` values
(R1.2, R1.3).

``route`` only *selects*. It returns a :class:`RoutingDecision` carrying the
chosen agent name and the routing reason; the runtime invokes the handler so
that the same trace/chaining machinery wraps every invocation uniformly. The
Orchestrator therefore requires no changes when new agents are added — it reads
whatever registry it was given at decision time (R2.4, R2.5).

Fallback routing (design §1, "Fallback routing"). Whenever routing cannot
confidently resolve a valid agent, the message is still processed — never
failed — by routing to the default agent with a distinct routing reason:

    ================= =========== ================================================
    Condition         Requirement routing_reason
    ================= =========== ================================================
    Selection > 30s   R1.5        "routing timed out; using default agent"
    Not valid JSON    R1.6        "malformed selection output; using default agent"
    Unknown agent     R1.7        "unknown agent name; using default agent"
    No confident match R1.8       "no confident match; using default agent"
    ================= =========== ================================================

"No confident match" is signalled by the model returning ``{"agent": "none"}``
(or an ``agent`` that is empty/absent while the output is otherwise valid JSON).
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass

from config import config

# A process-wide daemon executor used to run the blocking routing (Sonnet)
# invoke under a wall-clock guard. Unlike a ``with ThreadPoolExecutor()`` block
# -- whose ``__exit__`` joins on ``shutdown(wait=True)`` and so blocks until a
# stuck call finishes -- this shared executor is never joined. On a routing
# timeout we abandon the (possibly stuck) worker; its daemon thread never blocks
# interpreter exit, so ``route()`` returns within the budget (+ ~1s) (R1.5, C4).
_routing_executor = ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="routing-invoke"
)

# ---------------------------------------------------------------------------
# Routing constants
# ---------------------------------------------------------------------------

# The Bedrock selection budget (R1.4). If the model does not return a selection
# within this many seconds, the Orchestrator falls back to the default agent
# (R1.5).
ROUTING_TIMEOUT_S: float = 30.0

# The maximum length of the routing justification the model may return (R1.2).
# Longer reasons are truncated to keep the trace tidy.
MAX_REASON_CHARS: int = 200

# The number of most-recent conversation turns threaded into the routing prompt
# so the model can recognise an in-progress collection flow (context-aware
# routing). Bounded so the prompt stays small; only affects what the model sees,
# never how the decision is made (still pure LLM reasoning, R1.2/R1.3).
RECENT_CONTEXT_TURNS: int = 6

# Distinct routing_reason strings for each fallback case, copied verbatim from
# design §1 ("Fallback routing"). These strings are part of the contract; the
# router tests and trace panel depend on them being exact.
REASON_TIMEOUT: str = "routing timed out; using default agent"
REASON_MALFORMED: str = "malformed selection output; using default agent"
REASON_UNKNOWN_NAME: str = "unknown agent name; using default agent"
REASON_NO_MATCH: str = "no confident match; using default agent"

# Sentinel the model uses to signal it found no confident match (R1.8).
_NO_MATCH_TOKEN: str = "none"


@dataclass
class RoutingDecision:
    """The result of a routing decision (design §1).

    Attributes:
        agent: the selected agent's registry name. Always a name that is
            present in the registry snapshot used for the decision — either the
            model's confident choice or the default agent on any fallback.
        routing_reason: a single-sentence justification (<= 200 chars) for a
            confident selection, or one of the distinct fallback strings above
            when the default agent was used.
    """

    agent: str
    routing_reason: str


class Orchestrator:
    """Routes each user message to exactly one specialist agent (R1).

    The Orchestrator holds a reference to the agent registry (a dict mapping
    agent name -> ``{"description", "handler"}``) and a Bedrock runtime client.
    It reads the registry at decision time so newly registered agents become
    routable with no change here (R2.4, R2.5).
    """

    def __init__(self, registry: dict, bedrock, default_agent: str = "license_advisor"):
        """Construct an Orchestrator.

        Args:
            registry: the ``AGENT_REGISTRY`` dict (name -> {description, handler}).
            bedrock: a Bedrock Runtime client exposing ``invoke_model``.
            default_agent: the safe fallback agent used on any routing anomaly
                (R1.5-R1.8). Defaults to ``"license_advisor"``.
        """
        self._registry = registry
        self._bedrock = bedrock
        self._default_agent = default_agent

    def route(
        self,
        user_message: str,
        history: list[dict],
        last_agent: str | None = None,
    ) -> RoutingDecision:
        """Select the specialist agent for ``user_message`` (R1.2-R1.8).

        Only *selects* — never invokes the handler and never generates an
        answer (R1.9, R1.10). Never raises for a malformed, unknown, timed-out,
        or no-confidence selection: each of those degrades to the default agent
        with a distinct routing reason so the message is always processed
        (R1.5-R1.8).

        Context-aware routing: the most recent conversation turns and the
        ``last_agent`` (the agent that produced the previous assistant turn) are
        threaded into the routing prompt so the model can recognise an
        in-progress collection flow and keep a context-dependent follow-up with
        the same agent unless the user clearly changed topic (2.1-2.4). This
        changes only *what context the model sees* — the decision is still made
        purely by the model reasoning over the ``Agent_Description`` values, with
        no keyword or ``if/else`` selection (R1.2, R1.3). When there is neither
        history nor a ``last_agent`` (a first turn), the prompt is byte-for-byte
        identical to the original, so first-turn routing is unchanged (3.7).

        Args:
            user_message: the current user message.
            history: prior conversation turns (``{"role","content"}`` dicts).
                The most recent :data:`RECENT_CONTEXT_TURNS` are surfaced to the
                model as routing context.
            last_agent: the registry name of the agent selected on the previous
                turn of this session, or ``None`` for the first turn. Defaulted
                so existing positional callers ``route(message, history)`` are
                unaffected.

        Returns:
            A :class:`RoutingDecision` naming an agent present in the registry
            snapshot plus the routing reason.
        """
        # Snapshot the registry at decision time so the set of candidate agents
        # is fixed for this decision (R2.5). Only these names are eligible.
        candidates = {
            name: rec.get("description", "")
            for name, rec in self._registry.items()
        }

        recent_context = self._format_recent_context(history, last_agent)
        prompt = self._build_routing_prompt(candidates, recent_context)

        # --- Call Bedrock Sonnet within the 30s budget (R1.4, R1.5) ----------
        try:
            raw_output = self._invoke_sonnet(prompt, user_message)
        except FutureTimeout:
            # R1.5: selection did not complete within 30 seconds.
            return self._fallback(REASON_TIMEOUT)
        except Exception:  # noqa: BLE001 - any Bedrock/network error -> malformed
            # A failed/erroring call yields no parseable selection; treat it the
            # same as malformed output so the message is still processed (R1.6).
            return self._fallback(REASON_MALFORMED)

        # --- Parse strict JSON {"agent","reason"} (R1.2, R1.6) ---------------
        selection = self._parse_selection(raw_output)
        if selection is None:
            # R1.6: output is not valid JSON or lacks the expected shape.
            return self._fallback(REASON_MALFORMED)

        agent_name, reason = selection

        # R1.8: model explicitly signalled no confident match.
        if agent_name == "" or agent_name.lower() == _NO_MATCH_TOKEN:
            return self._fallback(REASON_NO_MATCH)

        # R1.7: the selected name is not present in the registry snapshot.
        if agent_name not in candidates:
            return self._fallback(REASON_UNKNOWN_NAME)

        # Confident, valid selection (R1.2). Bound the reason to 200 chars.
        return RoutingDecision(agent=agent_name, routing_reason=reason[:MAX_REASON_CHARS])

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _fallback(self, reason: str) -> RoutingDecision:
        """Build a RoutingDecision routing to the default agent (R1.5-R1.8)."""
        return RoutingDecision(agent=self._default_agent, routing_reason=reason)

    def _build_routing_prompt(
        self, candidates: dict[str, str], recent_context: str = ""
    ) -> str:
        """Build the routing system prompt from registry name/description only.

        The prompt lists each registered agent by ``name`` and its
        ``Agent_Description`` and asks the model to pick exactly one by
        reasoning over the descriptions (R1.2, R1.3). No keyword table or
        conditional branching participates in the decision.

        When ``recent_context`` is non-empty it is appended together with a
        routing instruction so the model can keep an in-progress collection flow
        with the same agent (context-aware routing, 2.1-2.4). ``recent_context``
        empty (first turn) yields the original prompt byte-for-byte (3.7).
        """
        lines = [
            "You are the routing brain for the Ask Gulf business-setup assistant.",
            "Choose exactly ONE specialist agent to handle the user's message by",
            "reasoning over the agent descriptions below. Do not answer the user.",
            "",
            "Available agents (name: description):",
        ]
        for name, description in candidates.items():
            lines.append(f"- {name}: {description}")
        if recent_context:
            lines.extend(
                [
                    "",
                    "Recent conversation context (most recent last):",
                    recent_context,
                    "",
                    "If the previous assistant turn was an agent collecting "
                    "information (asking a clarifying question or requesting an "
                    "application field), route this turn to that SAME agent "
                    "unless the user clearly changed topic. A short answer that "
                    "only makes sense as a reply to that question belongs with "
                    "the agent that asked it.",
                ]
            )
        lines.extend(
            [
                "",
                "Respond with STRICT JSON only, no prose, in exactly this shape:",
                '{"agent": "<agent name from the list above>", '
                '"reason": "<one sentence, <=200 characters>"}',
                "",
                'If no listed agent is a confident match, respond with '
                '{"agent": "none", "reason": "<why>"}.',
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _format_recent_context(
        history: list[dict] | None, last_agent: str | None
    ) -> str:
        """Render the recent turns + last-selected agent as routing context.

        Surfaces the most recent :data:`RECENT_CONTEXT_TURNS` turns as
        ``role: content`` lines and, when known, a line naming the agent that
        produced the previous assistant turn. Returns ``""`` when there is
        neither usable history nor a ``last_agent`` so the routing prompt is
        identical to the original for a first turn (3.7). Purely descriptive
        context for the model — no agent is selected here (R1.3).
        """
        lines: list[str] = []
        if isinstance(history, list) and history:
            recent = history[-RECENT_CONTEXT_TURNS:]
            for turn in recent:
                if not isinstance(turn, dict):
                    continue
                role = turn.get("role", "")
                content = turn.get("content", "")
                if not isinstance(content, str) or not content:
                    continue
                lines.append(f"{role}: {content}")
        if last_agent:
            lines.append(
                f"(The previous assistant turn was produced by the "
                f"'{last_agent}' agent.)"
            )
        return "\n".join(lines)

    def _invoke_sonnet(self, system_prompt: str, user_message: str) -> str:
        """Invoke Bedrock Sonnet for routing within the 30s budget (R1.4).

        Runs the blocking ``invoke_model`` call on a worker thread and enforces
        :data:`ROUTING_TIMEOUT_S`; a :class:`concurrent.futures.TimeoutError`
        propagates to the caller as the timeout fallback (R1.5). Returns the raw
        assistant text for JSON parsing.
        """
        body = json.dumps(
            {
                "anthropic_version": config.anthropic_version,
                "max_tokens": 256,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_message}],
            }
        )

        def _call() -> str:
            result = self._bedrock.invoke_model(
                modelId=config.sonnet_model_id,
                body=body,
            )
            return _extract_text(result)

        # Submit to the shared daemon executor and wait only up to the budget.
        # We deliberately do NOT use a ``with ThreadPoolExecutor()`` block: its
        # ``__exit__`` would join on shutdown and block until a stuck call
        # finishes. Abandoning the future here lets ``route()`` fall back within
        # the budget (+ ~1s); the orphaned daemon worker never delays the process.
        future = _routing_executor.submit(_call)
        # Raises concurrent.futures.TimeoutError if the budget is exceeded; the
        # caller's ``except FutureTimeout`` turns that into REASON_TIMEOUT (R1.5).
        return future.result(timeout=ROUTING_TIMEOUT_S)

    @staticmethod
    def _parse_selection(raw_output: str) -> tuple[str, str] | None:
        """Parse the model output into ``(agent, reason)`` or ``None`` (R1.6).

        Returns ``None`` when the output is not valid JSON, is not a JSON
        object, or is missing an ``agent`` field of the right type. A missing
        or empty ``reason`` defaults to an empty string (the caller bounds it).
        """
        if not isinstance(raw_output, str):
            return None
        text = raw_output.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        if "agent" not in parsed:
            return None
        agent = parsed.get("agent")
        if not isinstance(agent, str):
            return None
        reason = parsed.get("reason", "")
        if not isinstance(reason, str):
            reason = ""
        return agent.strip(), reason.strip()


def _extract_text(result) -> str:
    """Extract assistant text from a Bedrock ``invoke_model`` response.

    Mirrors the extraction in ``bedrock_client`` so the Orchestrator can drive
    Sonnet directly. Raises ``ValueError`` if no text is present so the caller
    treats it as a malformed selection (R1.6).
    """
    raw = result.get("body") if isinstance(result, dict) else None
    if raw is None:
        raise ValueError("bedrock response missing body")
    payload = raw.read() if hasattr(raw, "read") else raw
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if text:
            return text
    raise ValueError("bedrock response contained no text")
