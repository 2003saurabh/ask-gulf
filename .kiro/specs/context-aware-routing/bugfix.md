# Bugfix Requirements Document

## Introduction

The Orchestrator (`backend/orchestrator.py`) is a pure LLM router: it hands the user's message to Bedrock Sonnet, which reasons over the registered agents' descriptions and picks exactly one specialist. The router receives the conversation `history` on every call (`route(self, user_message, history)`) but ignores it — the docstring states history is "unused for routing", and `_invoke_sonnet(prompt, user_message)` sends only the current message to the model. As a result, every turn is classified in isolation.

This breaks multi-turn flows in which a specialist is mid-collection and the user's next turn is a short answer that only makes sense in context. A bare answer such as "fintech", "in next month", or "Gulf Tech LLC" has no standalone routing signal, so the model re-classifies the fragment from scratch and picks the wrong agent — abandoning the in-progress flow.

Two reproductions were observed in the running app:

- REPRO 1 (License Advisor multi-turn): the License Advisor asks for business activity, team size, and timeline. The user answers "fintech" (stays with license_advisor), then answers "in next month" — which is misrouted to `appointment_scheduler` (the router saw "next month" and booked SLOT-001), instead of staying with `license_advisor` to complete the recommendation. Logged reason: "User wants to schedule a meeting for next month, which requires checking calendar availability and booking a slot".
- REPRO 2 (Application Intake): `application_intake` asks "What is your company name?". The user answers "Gulf Tech LLC", which is misrouted to `compliance_screener` (the router saw a company name to "screen"), so the intake flow never progresses; subsequent answers scatter to other agents.

Root cause: routing is stateless/context-free. The fix threads recent conversation context (including which agent produced the last assistant turn) into the routing prompt so the router can recognise an in-progress flow and keep the follow-up turn with the same agent unless the user clearly changes topic. The runtime already loads history and passes it to `route(...)` in `backend/main.py` `run_foreground` — the plumbing exists; the orchestrator just needs to use it.

Scope and invariants:

- The router stays purely LLM-driven. No keyword or if/else agent selection is introduced (design §1, R1.2, R1.3). The fix changes what context the model sees, not how the decision is made.
- All existing routing and fallback behavior must be preserved. The four exact fallback reason strings, the confident-selection contract (≤200-char stripped reason), the `RoutingDecision` shape, and the routing-budget/timeout behavior (module-level daemon executor, no joining `with ThreadPoolExecutor()` block) must not regress. These are locked by the prior spec's Property-2 preservation tests (`backend/tests/test_preservation_latency_trace_fix.py`, §3.1).
- Only context-aware routing is in scope. The other latency-trace-fix defects (routing/total timings, CRM gating, client reuse, live trace) are out of scope and must not be touched.

References: R#.# is a requirement in `.kiro/specs/ask-gulf/requirements.md`. The preservation contract references §3 of `.kiro/specs/latency-trace-fix/bugfix.md`.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a follow-up user turn is a short answer to a clarifying question the License_Advisor asked (activity, team size, or timeline) and that answer contains a token that reads as another agent's intent in isolation (e.g. "in next month") THEN the system routes the turn to a different agent (e.g. appointment_scheduler) instead of back to the License_Advisor, abandoning the in-progress recommendation flow

1.2 WHEN a follow-up user turn is the answer to an Application_Intake field request (e.g. the company name "Gulf Tech LLC" answering "What is your company name?") THEN the system routes the turn to a different agent (e.g. compliance_screener) instead of back to application_intake, so the intake flow never progresses

1.3 WHEN the Orchestrator routes any turn THEN the system classifies only the current message text and does not consider the conversation history it was given, so it cannot tell that the previous assistant turn was an agent collecting information and cannot keep a context-dependent follow-up with that agent

### Expected Behavior (Correct)

2.1 WHEN a follow-up user turn answers a clarifying question the License_Advisor asked and the user has not clearly changed topic THEN the system SHALL route the turn to license_advisor so the recommendation flow continues (REPRO 1: after license_advisor asks for the timeline, "in next month" SHALL route to license_advisor, not appointment_scheduler)

2.2 WHEN a follow-up user turn answers an Application_Intake field request and the user has not clearly changed topic THEN the system SHALL route the turn to application_intake so the intake flow continues (REPRO 2: after application_intake asks for the company name, "Gulf Tech LLC" SHALL route to application_intake, not compliance_screener)

2.3 WHEN the Orchestrator routes any turn THEN the system SHALL make recent conversation context available to the routing decision — including which agent produced the last assistant turn — so the model can recognise an in-progress collection flow, while still selecting the agent purely by model reasoning with no keyword or if/else selection (R1.2, R1.3)

2.4 WHEN the previous assistant turn was an agent collecting information (asking a clarifying question or requesting an application field) AND the current user turn clearly changes topic THEN the system SHALL route to the agent that matches the new topic rather than being forced to stay with the previous agent

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the Orchestrator routes a message THEN the system SHALL CONTINUE TO pick one agent through Sonnet model reasoning over the registered Agent_Description values, with no keyword or if/else agent selection (R1.2, R1.3)

3.2 WHEN routing cannot confidently resolve a valid agent THEN the system SHALL CONTINUE TO route to the default agent with exactly one of these reasons, unchanged and byte-for-byte: "routing timed out; using default agent" (routing exceeds ROUTING_TIMEOUT_S), "malformed selection output; using default agent" (non-JSON / Bedrock error), "unknown agent name; using default agent" (name not in the registry snapshot), or "no confident match; using default agent" (`{"agent":"none"}` / empty agent) (R1.5–R1.8)

3.3 WHEN the model returns a confident, valid selection THEN the system SHALL CONTINUE TO route to the chosen agent with its one-sentence reason bounded to 200 characters and stripped (R1.2)

3.4 WHEN routing produces a decision THEN the system SHALL CONTINUE TO return a `RoutingDecision(agent, routing_reason)` whose `.agent` is a name present in the registry snapshot and whose `.routing_reason` matches the values above; the dataclass constructor and its `.agent`/`.routing_reason` fields SHALL CONTINUE TO be usable as they are today (any new field SHALL have a default so existing callers and tests are unaffected)

3.5 WHEN the Orchestrator's routing call runs longer than ROUTING_TIMEOUT_S THEN the system SHALL CONTINUE TO fall back to the default agent within the budget by running the Bedrock call on the module-level daemon executor and abandoning a stuck worker; `_invoke_sonnet` SHALL NOT reintroduce a `with ThreadPoolExecutor()` block that joins on exit (R1.5, prior-spec C4)

3.6 WHEN a message is routed to an agent THEN the runtime SHALL CONTINUE TO invoke the primary agent, run chaining, gate the CRM auto-fire, append exactly one user and one assistant turn, and report the trace exactly as it does today; this fix SHALL NOT change any of the latency-trace-fix behaviors (routing/total timings, CRM gating, AWS client reuse, live trace streaming)

3.7 WHEN a turn is a genuine new request or the first turn of a session (no in-progress collection flow) THEN the system SHALL CONTINUE TO route it by model reasoning exactly as today, so first-turn and topic-change routing are unchanged

## Deriving the Bug Condition

**Bug Condition** — identifies inputs that trigger the bug:

```pascal
FUNCTION isBugCondition(X)
  INPUT: X = (user_message, history) for one routing call
  OUTPUT: boolean

  // The previous assistant turn was an agent actively collecting information
  // (a License_Advisor clarifying question or an Application_Intake field
  // request), the current user_message is the in-context answer to it, and the
  // user did not clearly change topic — yet routing, seeing only user_message,
  // selects a different agent than the one that asked.
  RETURN lastAssistantTurnWasCollecting(history)
         AND userMessageIsInContextAnswer(X.user_message, history)
         AND NOT userClearlyChangedTopic(X.user_message, history)
         AND routeIgnoringHistory(X.user_message) != collectingAgent(history)
END FUNCTION
```

**Property (Fix Checking)** — desired behavior for buggy inputs:

```pascal
// Property: Fix Checking - keep the in-progress agent
FOR ALL X WHERE isBugCondition(X) DO
  decision <- route'(X.user_message, X.history)   // F' = fixed router
  ASSERT decision.agent = collectingAgent(X.history)
END FOR
```

**Preservation (Preservation Checking)** — non-buggy inputs unchanged:

```pascal
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT route(X) = route'(X)   // F (original) = F' (fixed): same agent + same routing_reason,
                                // including all four fallback reasons and confident selection
END FOR
```

- **F**: the original router (classifies only the current message; history unused).
- **F'**: the fixed router (recent history + last-agent context threaded into the routing prompt; still purely LLM-driven).
