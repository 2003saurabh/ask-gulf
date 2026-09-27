# Context-Aware Routing Bugfix Design

## Overview

The Orchestrator (`backend/orchestrator.py`) is a pure LLM router: it hands the
user's message to Bedrock Sonnet, which reasons over the registered agents'
descriptions and picks exactly one specialist. Today `route(self, user_message,
history)` receives the conversation `history` but ignores it — the docstring
says history is "unused for routing" and `_invoke_sonnet(prompt, user_message)`
sends only the current message. Every turn is therefore classified in isolation,
so a short in-context answer ("in next month", "Gulf Tech LLC") is re-classified
from scratch and misrouted, abandoning the in-progress specialist flow.

The fix keeps the router **purely LLM-driven** (no keyword / if-else selection)
and changes only *what context the model sees*. Two things are threaded into the
routing prompt:

1. **Recent conversation history** — the last few turns, so the model can see
   that the previous assistant turn was an agent collecting information.
2. **The last-selected agent** — passed into `route()` as a NEW DEFAULTED
   parameter `last_agent=None`, so the model knows which agent produced that
   previous assistant turn and can keep the follow-up with it.

A **routing instruction** is added to the prompt telling the model: if the
previous assistant turn was an agent collecting information (a clarifying
question or an application-field request), route this turn to that same agent
UNLESS the user clearly changed topic.

The runtime (`backend/main.py`) remembers the last-selected agent per session
and passes it into `route()`. `route(user_message, history)` stays
backward-compatible: `last_agent` defaults to `None`, existing positional
callers and tests are unaffected, and with no history/last-agent the prompt is
byte-for-byte identical to today, so first-turn routing does not change.

The session-store `{role, content}` contract and the `RoutingDecision(agent,
routing_reason)` shape are untouched.

## Glossary

- **Bug_Condition (C)**: The previous assistant turn was an agent actively
  collecting information (a License_Advisor clarifying question or an
  Application_Intake field request), the current user message is the in-context
  answer to it, the user did not clearly change topic, yet stateless routing
  (seeing only the current message) selects a different agent than the one that
  asked.
- **Property (P)**: For a buggy input, the fixed router keeps the turn with the
  agent that was collecting information (the "in-progress" agent).
- **Preservation**: For any non-buggy input, the fixed router produces the same
  `RoutingDecision` (agent + routing_reason) as today — including the four exact
  fallback reasons, confident selection with a ≤200-char stripped reason, and
  the module-level daemon-executor routing budget.
- **route()**: `Orchestrator.route(user_message, history, last_agent=None)` in
  `backend/orchestrator.py` — selects exactly one specialist by model reasoning.
- **last_agent**: The registry name of the agent selected on the previous turn
  of the same session; remembered by the runtime and passed into `route()`.
- **F**: the original (unfixed) router — classifies only the current message.
- **F'**: the fixed router — recent history + last_agent threaded into the
  routing prompt; still purely LLM-driven.

## Bug Details

### Bug Condition

The bug manifests when the previous assistant turn was an agent collecting
information and the current user turn is the short, in-context answer to it. The
router, seeing only the current message text, has no standalone signal and
re-classifies the fragment from scratch, picking a different agent than the one
that asked — abandoning the in-progress flow. The root cause is that `route()`
ignores the `history` it is given and never knows which agent produced the
previous turn.

**Formal Specification:**
```
FUNCTION isBugCondition(X)
  INPUT: X = (user_message, history, last_agent) for one routing call
  OUTPUT: boolean

  RETURN lastAssistantTurnWasCollecting(history)
         AND userMessageIsInContextAnswer(X.user_message, history)
         AND NOT userClearlyChangedTopic(X.user_message, history)
         AND routeIgnoringHistory(X.user_message) != collectingAgent(history)
END FUNCTION
```

### Examples

- REPRO 1: license_advisor asked for the timeline; last_agent="license_advisor".
  User answers "in next month". Expected: route to license_advisor. Actual
  (unfixed): appointment_scheduler (the router saw "next month" and booked a
  slot).
- REPRO 2: application_intake asked "What is your company name?";
  last_agent="application_intake". User answers "Gulf Tech LLC". Expected: route
  to application_intake. Actual (unfixed): compliance_screener (the router saw a
  company name to "screen").
- Topic-change (not a bug): mid-flow the user clearly changes topic ("actually,
  what are the compliance rules for a fintech?"). Expected: route to the agent
  matching the new topic, not forced to stay with the previous agent.
- First turn / no history: no in-progress flow. Expected: route by model
  reasoning exactly as today.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- Routing stays purely LLM-driven — one agent chosen by Sonnet reasoning over
  the registered `Agent_Description` values, with no keyword or if/else
  selection (R1.2, R1.3).
- The four fallback reason strings are byte-for-byte unchanged, and the
  conditions that trigger them (timeout, malformed/Bedrock error, unknown name,
  no-confident-match) are unchanged (R1.5–R1.8).
- Confident selection returns the chosen agent with a one-sentence reason
  bounded to 200 characters and stripped (R1.2).
- `RoutingDecision(agent, routing_reason)` shape is unchanged and usable
  positionally.
- The routing budget is unchanged: Bedrock runs on the module-level daemon
  executor; `_invoke_sonnet` does NOT reintroduce a joining
  `with ThreadPoolExecutor()` block (R1.5, prior-spec C4).
- No latency-trace-fix behavior changes (routing/total timings, CRM gating, AWS
  client reuse, live trace streaming) — C1/C2/C3/C5/C6 untouched.
- The session-store `{role, content}` turn contract is unchanged.

**Scope:**
All inputs that do NOT satisfy the bug condition are unaffected. This includes:
- First-turn routing (no history, `last_agent=None`).
- Genuine new requests and clear topic changes mid-flow.
- All fallback paths (timeout, malformed, unknown, no-match).
- Existing callers/tests that call `route(message, history)` positionally.

## Hypothesized Root Cause

Based on the bug analysis, the cause is definite (confirmed by the code):

1. **History ignored in routing**: `route()` receives `history` but never uses
   it; the docstring states it is "unused for routing".
2. **Only the current message reaches the model**: `_invoke_sonnet` sends
   `messages=[{"role":"user","content": user_message}]` — the model never sees
   prior turns or which agent produced them.
3. **No notion of the in-progress agent**: nothing tells the router that the
   previous assistant turn was an agent mid-collection, so a context-dependent
   follow-up cannot be kept with that agent.

## Correctness Properties

Property 1: Bug Condition - Keep the in-progress agent on a context-dependent follow-up

_For any_ input where the bug condition holds (isBugCondition returns true) — the
previous assistant turn was an agent collecting information, the current user
turn is the in-context answer, and the user did not clearly change topic — the
fixed `route()` SHALL make the recent history and the last-selected agent
available to the routing decision and SHALL route the turn to the agent that was
collecting information (REPRO 1 → license_advisor; REPRO 2 → application_intake).
When the user clearly changes topic mid-flow, `route()` SHALL instead follow the
new topic. Selection remains purely model reasoning with no keyword or if/else
branching.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4**

Property 2: Preservation - Stateless routing outcomes unchanged

_For any_ input where the bug condition does NOT hold (isBugCondition returns
false) — first turns, genuine new requests, clear topic changes, and all
fallback paths — the fixed `route()` SHALL produce the same `RoutingDecision`
(same `.agent` and same `.routing_reason`) as the original router, preserving the
four exact fallback reasons, the ≤200-char stripped confident reason, the
`RoutingDecision` shape, the positional-call contract, and the module-level
daemon-executor routing budget.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

**File**: `backend/orchestrator.py`

**Function**: `Orchestrator.route`, `Orchestrator._build_routing_prompt`,
`Orchestrator._invoke_sonnet`

**Specific Changes**:

1. **New defaulted parameter on `route()`**: change the signature to
   `route(self, user_message, history, last_agent=None)`. `last_agent` is the
   registry name of the agent selected on the previous turn of this session (or
   `None` for the first turn). Positional callers `route(message, history)` keep
   working unchanged.

2. **Build a "recent context" block from history**: add a helper
   `_format_recent_context(history, last_agent)` that renders the last few turns
   (a small cap, e.g. the last 6 turns) as `role: content` lines and, when
   `last_agent` is set, a line naming the agent that produced the previous
   assistant turn. When there is no history AND no last_agent, this helper
   returns an empty string so the prompt is byte-for-byte identical to today
   (preserves first-turn routing, 3.7).

3. **Thread the context + routing instruction into the prompt**: `route()` passes
   the recent context into `_build_routing_prompt(candidates, recent_context)`.
   When `recent_context` is non-empty, the prompt appends: the recent
   conversation, a line naming the last-selected agent, and a routing
   instruction — "If the previous assistant turn was an agent collecting
   information (a clarifying question or an application-field request), route
   this turn to that SAME agent unless the user clearly changed topic." The
   instruction is guidance to the model only; the model still returns the same
   strict-JSON `{"agent","reason"}` and the decision is still purely its
   reasoning (no keyword/if-else in our code).

4. **Send recent turns to the model**: `_invoke_sonnet` continues to send the
   current user message. The recent context is carried through the *system
   prompt* (via `_build_routing_prompt`) so the model reasons over it. This keeps
   `_invoke_sonnet`'s executor/timeout path untouched — the module-level daemon
   executor and `future.result(timeout=ROUTING_TIMEOUT_S)` are unchanged, and no
   `with ThreadPoolExecutor()` block is introduced (3.5, C4).

5. **Update docstrings**: `history` is no longer "unused for routing"; document
   `last_agent` and that with neither history nor last_agent the behavior is
   identical to today.

**File**: `backend/main.py`

**Function**: `run_foreground` (+ a small per-session last-agent store)

**Specific Changes**:

6. **Remember the last-selected agent per session**: add a process-wide,
   thread-safe mapping `session_id -> last_agent` (a dict guarded by a lock,
   mirroring the existing `_s3_client_singleton` pattern) with a
   `reset_last_agent_cache()` test seam. In `run_foreground`, read the remembered
   `last_agent` for the session BEFORE routing and pass it into
   `orch.route(user_message, history, last_agent=last_agent)`. After the decision
   is made, record `decision.agent` as the session's last agent so the NEXT turn
   sees it.

   Rationale for a runtime-side store: history turns are stored as `{role,
   content}` with no agent attribution (a contract we must not change, 3.6/§store).
   The runtime is the component that knows which agent was selected each turn, so
   it is the natural owner of the per-session last-agent memory. The value is
   passed to `route()` explicitly rather than inferred from history, keeping the
   store contract intact.

## Testing Strategy

### Validation Approach

Two phases: first surface a counterexample that demonstrates the bug on the
UNFIXED code, then verify the fix works and preserves existing behavior. All AWS
(Bedrock, DynamoDB, S3) is mocked with injected fakes so tests never call real
AWS and are deterministic.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing
the fix, and confirm the root cause (history + last-agent are not threaded into
the routing prompt).

**Test Plan**: Use an injected **fake Sonnet** that inspects the routing prompt
(system prompt / body) it is given and returns a decision based on whether the
recent history and the last agent are present:
- If the prompt does NOT contain the recent history and last agent (the UNFIXED
  behavior), the fake returns the "stateless misroute" agent (appointment_scheduler
  for REPRO 1, compliance_screener for REPRO 2) — modelling the real model's
  isolated re-classification.
- If the prompt DOES contain them (the FIXED behavior), the fake returns the
  in-progress agent (license_advisor / application_intake).

This makes the assertion deterministic and tied to the fix's observable
contract: the exploration test asserts the routing prompt INCLUDES the recent
history and the last agent AND that the resulting decision is the in-progress
agent. On UNFIXED code the prompt lacks that context, so the fake returns the
misroute and the test FAILS (confirming the bug). After the fix the context is
present, the fake returns the in-progress agent, and the test PASSES.

**Test Cases**:
1. **REPRO 1 (license_advisor timeline)**: history where the last assistant turn
   is license_advisor asking for the timeline, last_agent="license_advisor", user
   turn "in next month" → route to license_advisor (FAILS on unfixed).
2. **REPRO 2 (application_intake company name)**: history where the last
   assistant turn is application_intake asking for the company name,
   last_agent="application_intake", user turn "Gulf Tech LLC" → route to
   application_intake (FAILS on unfixed).
3. **Topic-change (2.4)**: mid-flow, the user clearly changes topic; the fake
   returns the new-topic agent regardless — routing follows the new topic rather
   than being forced to stay. (Passes on unfixed and fixed; documents the
   guard.)

**Expected Counterexamples**:
- On unfixed code the routing prompt contains neither the recent history nor the
  last agent, so the fake models the isolated misroute: "in next month" →
  appointment_scheduler; "Gulf Tech LLC" → compliance_screener.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed
router keeps the in-progress agent.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  decision := route_fixed(input.user_message, input.history, input.last_agent)
  ASSERT decision.agent = collectingAgent(input.history)
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the
fixed router produces the same result as the original router.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT route_original(input) = route_fixed(input)   // same agent + same routing_reason
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation
because preservation is a universal property over the input domain; PBT
generates many cases automatically and catches edge cases manual tests miss. The
existing §3.1 routing preservation tests in
`backend/tests/test_preservation_latency_trace_fix.py` already exercise confident
selection and all four fallback reasons via `orch.route(message, [])` positionally
— reused/extended here as the preservation baseline (they must continue to pass
unchanged, proving `last_agent`'s default keeps stateless routing identical).

**Test Plan**: Reuse the existing §3.1 preservation tests (they pass on unfixed
code and must keep passing). Extend with a check that calling
`route(message, [])` positionally (no last_agent) yields the same decision as
before, and that an empty-history / no-last-agent call produces an unchanged
prompt path.

**Test Cases**:
1. **Confident selection preserved**: valid model selection → that agent with the
   ≤200-char stripped reason (existing §3.1).
2. **Four fallback reasons preserved**: timeout, malformed/error, unknown name,
   no-match → default agent with the exact reason strings (existing §3.1).
3. **Positional call preserved**: `route(message, [])` still works and is
   unchanged.

### Unit Tests

- Route keeps the in-progress agent for REPRO 1 and REPRO 2 (with fake Sonnet).
- Route follows a clear topic change mid-flow (2.4).
- `_build_routing_prompt` with empty recent context is byte-for-byte the same as
  today (first-turn preservation).
- `run_foreground` remembers `decision.agent` per session and passes the prior
  turn's agent into the next `route()` call.

### Property-Based Tests

- Preservation: confident selection and all four fallbacks unchanged across many
  generated messages/agents (existing §3.1, reused).
- Bug condition: across generated in-context follow-ups where the prompt carries
  history + last_agent, the decision is the in-progress agent.

### Integration Tests

- `run_foreground` over two turns of the same session: turn 1 routes to
  license_advisor (which asks a clarifying question); turn 2's short answer,
  with last_agent threaded, stays with license_advisor.
- Switching sessions does not leak the remembered last agent across session ids.
