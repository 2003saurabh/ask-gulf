# Latency & Trace Fix — Bugfix Design

## Overview

Ask Gulf runs locally against real Bedrock and DynamoDB, and its replies are slow while its trace misbehaves. For one observed message the Trace_Panel showed `license_advisor` at 1825 ms and `crm_agent` at 225 ms, and the chat reply ended with a spurious "Lead LEAD-... captured" line — added even to a greeting like "hello". Six defects combine to cause this (bugfix.md §1):

- **C1 — Missing timings.** Routing time (the Sonnet call in `orchestrator.route`) and total turn time are never measured, so the trace under-reports the wait.
- **C2 — CRM in the foreground.** `crm_auto_fire` runs synchronously inside `process_message` and its confirmation is appended to the reply, adding its time (225 ms observed) and its text to every License_Advisor reply.
- **C6 — CRM fires on every License_Advisor turn.** The hook only checks `selected_agent == "license_advisor"`, so a greeting, clarifying question, fallback, or contract-rejected response still writes a lead and adds a CRM trace.
- **C3 — AWS client churn.** Bedrock, DynamoDB, and S3 clients are re-created on every call (106–167 ms per Bedrock client, 139–245 ms per DynamoDB Table locally), re-resolving SSO credentials and opening a new TLS connection each time.
- **C4 — Budgets not enforced.** `invoke_agent`'s `timeout_s` is never applied, clients carry no socket timeouts, and the routing executor blocks on shutdown after a timeout, so a slow Bedrock call can hold a turn for a minute or more.
- **C5 — Buffered trace / blocked loop.** The `/ws` handler buffers every trace event and flushes them only after the whole (synchronous) turn finishes, blocking the event loop for the turn's duration.

The fix is deliberately **contained and low-risk** (the AWS Summit demo is 30 September 2026): no new heavy dependencies, the existing module layout preserved. It removes avoidable overhead and surfaces the true total time. It cannot guarantee sub-3-second turns when the routing and License_Advisor model calls alone exceed that.

The general strategy:

1. Time routing and the whole turn in `process_message`, and carry those two numbers alongside the trace as report-only fields (no extra Trace_Entry).
2. Introduce a **recommendation signal** from the License_Advisor (an additive, non-user-visible field) so the CRM hook fires only on a recommendation.
3. Run the CRM step in the **background** on the WebSocket path — reply first, CRM trace when it completes — while `/invocations` keeps waiting for it.
4. Reuse process-wide, thread-safe AWS clients created once behind a lock.
5. Enforce per-call budgets with SDK socket timeouts plus a wall-clock guard, degrading within ≤ 1 s of the budget.
6. Stream each trace event the instant it is produced and run the blocking work off the event loop so other connections and `/health` / `/ping` stay responsive.

## Glossary

- **Bug_Condition (C)**: the condition under which a defect manifests. Each defect (C1–C6) has its own predicate; collectively `isBugCondition(input)` is true when a processed message triggers any of the six defects.
- **Property (P)**: the desired behavior once fixed — accurate timings, background non-blocking CRM, CRM only on recommendations, reused clients, enforced budgets, and live streamed trace.
- **Preservation**: existing behavior that must stay identical — routing semantics, the response contract, fallback degradation, Trace_Entry shape, the WebSocket protocol, history-turn accounting, chaining, per-agent rules, and credential resolution (bugfix.md §3).
- **Recommendation**: a License_Advisor reply that recommends exactly one of the four packages with its annual AED cost (R4.2, R4.3) **or** offers a custom quote (R4.5). A clarifying question, a greeting, a packages-mention-without-a-pick, a Fallback_Response, and a contract-rejected response are **not** recommendations.
- **process_message** (`backend/main.py`): the framework-agnostic per-message pipeline (route → primary agent → CRM auto-fire → chaining → concatenate → append history).
- **crm_auto_fire** (`backend/runtime.py`): the post-processing hook that fires `crm_agent` after `license_advisor`.
- **Routing time / Total time**: the wall-clock milliseconds for the Orchestrator's Sonnet call, and for the whole turn from message received to reply sent (excluding any background CRM step). Reported alongside the trace; **not** a Trace_Entry.
- **Report-only trace fields**: additive fields on the `response` event (and per-event turn correlation) that the frontend renders without a Trace_Entry and that keep the existing event shapes consumable (R3.5-frontend).

## Bug Details

### Bug Condition

The system misbehaves when a message is processed: timings are missing (C1), the CRM step runs in the foreground and pollutes the reply (C2), the CRM fires without a recommendation (C6), AWS clients are re-created per call (C3), call budgets are not enforced (C4), and trace events are withheld while the event loop is blocked (C5).

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type ProcessedMessage
         (session_id, user_message, transport ∈ {ws, invocations},
          routing_outcome, primary_agent, primary_response, is_recommendation)
  OUTPUT: boolean

  # C1 — routing time and total turn time are not measured/reported
  c1 := NOT reportsRoutingTime(input) OR NOT reportsTotalTime(input)

  # C2 — CRM runs in the foreground and its text is appended to the reply
  c2 := primary_agent == "license_advisor"
        AND crmRunsBeforeReply(input)
        AND crmTextAppendedToReply(input)

  # C6 — CRM fires when the License_Advisor reply is NOT a recommendation
  c6 := primary_agent == "license_advisor"
        AND NOT input.is_recommendation
        AND crmInvoked(input)

  # C3 — AWS client/credentials/connection re-created for this call
  c3 := recreatesAwsClientPerCall(input)

  # C4 — a Bedrock/routing call exceeds its budget but is not cut off in time
  c4 := (callExceedsBudget(input) AND NOT degradesWithinOneSecond(input))

  # C5 — trace events withheld until turn end, and the loop is blocked
  c5 := traceBufferedUntilReply(input) OR blocksEventLoopDuringTurn(input)

  RETURN c1 OR c2 OR c6 OR c3 OR c4 OR c5
END FUNCTION
```

The **non-buggy** inputs to preserve are all message-processing behaviors that do not trip any of C1–C6: routing decisions and their reasons, contract rejection, fallback degradation, Trace_Entry contents, the WebSocket frame protocol, history turns, chaining, per-agent rules, and credential source (bugfix.md §3).

### Examples

- **Greeting writes a lead (C2, C6).** User sends "hello"; routing defaults to `license_advisor`, which returns a greeting. Today: a lead `LEAD-...` is written, a CRM Trace_Entry appears, and "Lead ... captured..." is appended to the reply. Expected: no CRM invocation, no lead, no CRM trace, and no CRM text in the reply.
- **Recommendation (C2).** User completes the three attributes; License_Advisor recommends "Startup at AED 22,000/year". Today: reply is delayed ~225 ms by the foreground CRM and ends with the lead line. Expected: reply sent immediately without CRM text; the CRM Trace_Entry (identifying the lead id) arrives separately, before or after the reply.
- **Under-reported wait (C1).** Trace shows `license_advisor` 1825 ms but nothing for the ~routing Sonnet call or the total turn. Expected: routing time and total time reported alongside the trace and shown in the panel, with no extra Trace_Entry.
- **Slow Bedrock (C4).** A License_Advisor Bedrock call hangs; today it waits out the SDK's 60 s read timeout across retries. Expected: it degrades to the Fallback_Response no more than 1 s after its 10 s budget.
- **Client churn (C3).** Each Bedrock/DynamoDB/S3 call rebuilds its client (100–245 ms + credential + TLS). Expected: a process-wide client is reused.
- **Buffered trace (C5, edge).** While a turn runs, a second connection's `/health` probe and the client's trace updates wait until the turn finishes. Expected: trace streams live and other requests are served concurrently.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors** (bugfix.md §3):

- Routing still selects exactly one agent through Sonnet reasoning over `Agent_Description` values, with the four exact fallback reasons on an anomaly (3.1).
- A contract-violating response (primary, chained, or CRM) is still rejected — no reply text, no Trace_Entry (3.2).
- Bedrock errors, missing/expired credentials, and a CRM DynamoDB write failure still degrade to the predefined Fallback_Response with `tools_used == ["fallback-mode"]` (or the Compliance_Screener's non-model parse) without failing the message (3.3).
- Every Trace_Entry still carries `agent_selected`, `routing_reason`, a `tools_used` list, and a non-negative integer `time_ms` **measuring only that agent's own step**; the primary entry still carries the routing reason (3.4).
- `/ws/{session_id}` still accepts `{"text": ...}`, ignores empty/malformed frames without dropping the connection, and sends incremental `{"type":"trace","entry":...}` events plus one `{"type":"response","message":...,"trace":[...]}` event in a form the existing frontend consumes (3.5).
- A reply still appends exactly one user turn and one assistant turn containing the reply text; the background CRM step appends no turn (3.6).
- `chain_next` still invokes named agents in order, appends their text, emits one Trace_Entry per step, stops after five distinct agents or a repeat, and skips a missing agent (3.7).
- A recommendation still yields exactly two Trace_Entry records (License_Advisor then CRM_Agent) and one NEW lead with a `LEAD-<timestamp>` id, `source aws-summit-demo`, an interaction summary, and a creation timestamp; the panel shows both whether the CRM entry arrives before or after the reply (3.8).
- A message routed to any agent other than License_Advisor (including CRM directly) runs that agent as primary, includes its text, and does not auto-fire CRM (3.9).
- `POST /invocations` still returns HTTP 200 with reply text and the complete trace, including the CRM entry when CRM was invoked; a processing error still returns a safe fallback at HTTP 200 (3.10).
- `GET /ping`, `GET /health`, and `POST /upload-document` still behave as specified (3.11).
- Each Specialist_Agent still follows its existing rules — the License_Advisor's packages, AED costs, 149-word cap, clarifying questions, and custom quotes are **unchanged in user-visible text** (3.12).
- Credentials still come from the SSO profile locally and the IAM/AgentCore execution role when deployed, with none in source (3.13).

**Scope:**
All inputs that do NOT trip C1–C6 must be completely unaffected. In particular:

- Any message routed to `application_intake`, `compliance_screener`, `appointment_scheduler`, or `crm_agent` directly.
- Any License_Advisor turn that is not a recommendation (its reply text is unchanged; only the removed CRM side effects differ).
- The exact bytes of every Trace_Entry and of the `message` and `trace` fields on the `response` event.

The correct behavior for buggy inputs is defined in the Correctness Properties section (Property 1).

## Hypothesized Root Cause

Based on the defect analysis and code inspection:

1. **No timing instrumentation around routing or the turn (C1).** `process_message` calls `orch.route(...)` untimed; the primary Trace_Entry's `time_ms` comes only from the agent response, and there is no measurement from message-received to reply-sent.

2. **Synchronous CRM with text concatenation (C2).** `process_message` calls `crm_auto_fire(...)` inline and appends `crm_response["text"]` to `texts`, so the CRM step's latency and its confirmation both land on the reply path.

3. **Too-loose CRM trigger and no recommendation signal (C6).** `crm_auto_fire` fires whenever `selected_agent == "license_advisor"`. The only nearby signal, `license_advisor._detect_recommended_package`, matches any package name as a whole word and returns `None` when "custom quote" appears — too loose for the bugfix.md definition and silent for custom quotes (which must create a lead). There is no explicit "this reply is a recommendation" signal on the response.

4. **Per-call client construction (C3).** `bedrock_client.invoke_agent` calls `_default_bedrock_client()` every call; `compliance_screener`, `crm_agent._default_table()`, `application_intake._default_dynamodb_table()`, and `main._default_s3_client()` do the same, each re-resolving credentials and opening a new connection.

5. **Budgets defined but not enforced (C4).** `_bedrock_invoke` ignores `timeout_s`; clients have no `botocore.config.Config` (connect/read timeouts, retries); and `orchestrator._invoke_sonnet` uses a `with ThreadPoolExecutor(...)` block whose `shutdown(wait=True)` blocks after `future.result(timeout=...)` raises, delaying the timeout fallback until the stuck call returns.

6. **Buffered trace and a blocked loop (C5).** The `/ws` handler collects events in a `pending` list flushed only after the synchronous `process_message` returns, and runs that blocking function directly inside the async handler, stalling the event loop for the whole turn.

## Correctness Properties

Property 1: Bug Condition — Timings, background CRM, recommendation-gated lead, reused clients, enforced budgets, and live trace

_For any_ processed message where the bug condition holds (`isBugCondition` returns true), the fixed system SHALL:
- report the Orchestrator's routing time (including on routing fallback) and the total turn time (excluding any background CRM step) alongside that message's trace, and the Trace_Panel SHALL display both **without** adding a Trace_Entry (2.1, 2.2);
- on the WebSocket path, send the reply without waiting for the CRM_Agent, run the CRM_Agent in the background for the same message, and send its Trace_Entry when it completes (before or after the reply), such that the background step neither delays the next message nor, on failure, fails the reply or closes the connection, and the lead is still recorded if the client disconnects first (2.3);
- invoke the CRM_Agent, create a lead, and produce a CRM Trace_Entry **only** when the License_Advisor reply is a recommendation, and never otherwise, keeping the CRM confirmation out of the reply text and identifying the created lead in the CRM Trace_Entry (2.4, 2.5);
- reuse process-wide AWS client setup instead of re-creating clients, credentials, and connections per call, remaining safe under concurrent calls (2.6);
- stop waiting no more than 1 second after a call's budget elapses and degrade as specified — the License_Advisor to its Fallback_Response with `tools_used ["fallback-mode"]`, the Compliance_Screener still returning within 5 seconds, and routing to the default agent with reason "routing timed out; using default agent" (2.7, 2.8);
- send each Trace_Entry as soon as its step completes and keep serving other WebSocket connections and HTTP requests (including `/health` and `/ping`) during a turn (2.9, 2.10).

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10**

Property 2: Preservation — Non-buggy inputs behave exactly as before

_For any_ input where the bug condition does NOT hold (`isBugCondition` returns false), the fixed system SHALL produce the same result as the original system, preserving routing semantics and fallback reasons, contract rejection, degradation on AWS failure, the Trace_Entry shape and per-step `time_ms`, the WebSocket frame protocol, history-turn accounting, chaining behavior, the two-entry/one-lead recommendation flow, non-License_Advisor routing, the `/invocations` and health/upload endpoints, every Specialist_Agent's user-visible rules, and credential resolution.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13**

## Fix Implementation

### Changes Required

Assuming the root-cause analysis is correct, the changes are grouped by defect. All are additive or internal; no user-visible text or protocol field is removed.

**1. Routing & total timing (C1) — `backend/main.py`, `backend/orchestrator.py`**

- In `process_message`, wrap `orch.route(...)` with `time.perf_counter()` to produce `routing_ms` (measured even when routing falls back). Measure `total_ms` from the start of processing to just before the reply is returned, excluding any background CRM work.
- Carry `routing_ms` and `total_ms` as **report-only** additive fields on the final `response` payload (e.g. `response["routing_ms"]`, `response["total_ms"]`), not as Trace_Entry records. Log `total_ms` (2.2).
- Optionally expose routing time from `Orchestrator.route` via a `RoutingDecision` field or a returned duration, keeping the existing `agent`/`routing_reason` intact so 3.1/3.4 are preserved.

**2. Background, recommendation-gated CRM (C2, C6) — `backend/runtime.py`, `backend/agents/license_advisor.py`, `backend/main.py`**

- Add an explicit, additive recommendation signal on the License_Advisor response — e.g. `structured_data["is_recommendation"] = True` set when the reply recommends exactly one package with its AED cost **or** offers a custom quote. Derive it from the existing `_detect_recommended_package` result **plus** an explicit custom-quote check, without changing the reply text (3.12). A fallback or clarifying reply leaves it false/absent.
- Change `crm_auto_fire` to fire only when `selected_agent == "license_advisor"` AND the primary response signals a recommendation. Keep it a no-op (no lead, no trace) otherwise (2.4).
- Keep the CRM confirmation out of `texts`; the lead capture is surfaced solely through the CRM Trace_Entry, which identifies the lead id (2.5). Ensure the emitted CRM Trace_Entry (or the CRM response's `structured_data`) carries the `LEAD-...` id.
- Split `process_message` so it returns the reply as soon as the primary + chain complete, exposing a separate callable for the CRM step that the transport layer schedules (foreground for `/invocations`, background for `/ws`).

**3. Non-blocking background CRM on the WebSocket (C2/C5) — `backend/main.py`**

- After sending the `response`, schedule the CRM step as an asyncio background task (e.g. `asyncio.create_task`, running the blocking CRM work via `run_in_executor`). Emit its Trace_Entry through the same sender when it completes.
- Serialize all WebSocket sends through a single sender task fed by an `asyncio.Queue` so the reply and a possibly-later CRM trace never interleave on one Starlette socket.
- Guard the background task so a CRM failure is logged and swallowed (never fails the reply or closes the socket), and so the lead write still completes if the client has disconnected (do not cancel on disconnect; detach the write from the send).
- Tag events with a per-turn correlation id (additive field) so a late CRM trace can be reconciled to its message even after the next message is sent (see Frontend).

**4. Reused AWS clients (C3) — `backend/bedrock_client.py`, `backend/main.py`, `backend/agents/*`**

- Introduce process-wide, lazily-created singletons behind a `threading.Lock` (double-checked) so two threads don't race on first construction:
  - a single **bedrock-runtime** low-level client (thread-safe once created) reused by `invoke_agent` and the Compliance_Screener parse;
  - a single **S3** low-level client for uploads;
  - for **DynamoDB**, pick a thread-safe strategy: a low-level `dynamodb` client shared across threads with `boto3.dynamodb.types.TypeSerializer` for `put_item`, **or** thread-local `boto3.resource`/Table objects (resources and Sessions are not thread-safe and must not be shared). The design prefers the shared low-level client + `TypeSerializer` to avoid per-thread churn.
- Keep the existing SSO-profile-vs-execution-role selection so 3.13 is preserved; only the caching changes. Accept injected clients/tables in tests exactly as today.

**5. Enforced budgets (C4) — `backend/bedrock_client.py`, `backend/orchestrator.py`**

- Attach a `botocore.config.Config(connect_timeout=..., read_timeout=..., retries={"max_attempts": ...})` to the reused clients so per-socket operations bound the wait.
- In `_bedrock_invoke`, actually apply `timeout_s` — run the blocking call under a wall-clock guard (e.g. a single-worker executor with `future.result(timeout=timeout_s + ε)`), so the agent degrades to fallback no more than ~1 s after the budget (2.7). The License_Advisor keeps its 10 s budget; the Compliance_Screener's parse budget stays small enough to leave headroom for its 5 s total (falling back to non-model parsing on timeout, 3.3).
- In `orchestrator._invoke_sonnet`, avoid blocking on executor shutdown after a timeout: use a module-level/shared executor (or `shutdown(wait=False)` on 3.9+) so the `REASON_TIMEOUT` fallback returns within ~1 s of the 30 s budget (2.8).

**6. Live trace + concurrency (C5) — `backend/main.py`**

- Run `process_message` off the event loop (`await loop.run_in_executor(...)`) so the async handler does not block other connections and `/health` / `/ping` stay responsive (2.10).
- Stream each Trace_Entry as it is produced instead of buffering: the sync `emit_trace` callback enqueues onto the sender queue (thread-safe hand-off via `run_coroutine_threadsafe` / `call_soon_threadsafe`), and the sender task forwards frames in order the instant they arrive (2.9). The final `response` (with `routing_ms`/`total_ms`) is enqueued after the reply text is ready.

### Files touched

- `backend/main.py` — timing, background CRM task, single sender/queue, off-loop execution, report-only fields, per-turn id.
- `backend/runtime.py` — recommendation-gated `crm_auto_fire`; CRM text no longer joins the reply.
- `backend/orchestrator.py` — expose routing time; non-blocking timeout fallback.
- `backend/bedrock_client.py` — cached clients, `botocore.config.Config`, applied `timeout_s` wall-clock guard.
- `backend/agents/license_advisor.py` — additive `is_recommendation` signal (no reply-text change).
- `backend/agents/compliance_screener.py`, `backend/agents/crm_agent.py`, `backend/agents/application_intake.py` — use the cached clients/tables.
- `frontend/src/components/ChatPanel.tsx`, `TracePanel.tsx`, `TraceCard.tsx`, `frontend/src/types.ts` — render routing/total time; reconcile a late CRM trace via the per-turn id (additive, existing shapes still consumable).

## Testing Strategy

### Validation Approach

Two phases: first surface counterexamples that demonstrate each defect on the **unfixed** code, then verify the fix works and preserves existing behavior. All AWS boundaries (Bedrock, DynamoDB, S3) are mocked (stubs/injected fakes or moto) — tests never call real AWS. Backend tests run with `.\.venv\Scripts\python.exe -m pytest` from `backend/` (pytest + Hypothesis installed; `conftest.py` puts `backend/` on `sys.path`). Frontend verification uses `npm run type-check` and `npm run build`.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the six defects BEFORE implementing the fix, confirming or refuting the root-cause analysis. If refuted, re-hypothesize.

**Test Plan**: Drive `process_message` and the WebSocket handler with injected fakes (a fake Orchestrator/Bedrock, a fake DynamoDB table, a controllable slow Bedrock stub) and assert on the observable defects. Run first on the UNFIXED code to observe failures.

**Test Cases**:
1. **Missing timings (C1)**: process a message and assert the `response` payload carries routing and total time (will fail on unfixed code — fields absent).
2. **CRM foreground + reply pollution (C2)**: on a recommendation, assert the reply text contains no "Lead ... captured" line and the reply is emitted before the CRM trace (will fail on unfixed code).
3. **CRM on a non-recommendation (C6)**: process a greeting routed to `license_advisor`; assert no lead written, no CRM Trace_Entry, no CRM text (will fail — a lead is written today).
4. **Client churn (C3)**: patch the client factory with a call counter; process several messages and assert the factory is invoked once (will fail — invoked per call).
5. **Budget not enforced (C4)**: inject a Bedrock stub that sleeps beyond the budget; assert degradation within ~1 s of the budget for the License_Advisor and within ~1 s of 30 s for routing (will fail — waits far longer today).
6. **Buffered trace / blocked loop (C5, edge)**: assert trace frames arrive incrementally and a concurrent `/ping` responds during a slow turn (may fail on unfixed code).

**Expected Counterexamples**:
- No routing/total time reported; CRM text on the reply; a lead written for a greeting; the client factory called per call; a stuck call held for the SDK's default timeout; trace frames delivered only at turn end.
- Possible causes: untimed routing, synchronous CRM with text concat, over-loose CRM trigger, per-call client construction, unapplied `timeout_s`, buffered `pending` flush inside a blocking handler.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed system produces the expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  result := processMessage_fixed(input)
  ASSERT reportsRoutingTime(result) AND reportsTotalTime(result)          # 2.1, 2.2
  ASSERT (input.primary_agent == "license_advisor" AND input.is_recommendation)
         IMPLIES crmRanInBackground(result) AND crmTextNotInReply(result) # 2.3, 2.5
  ASSERT (input.primary_agent == "license_advisor" AND NOT input.is_recommendation)
         IMPLIES noLead(result) AND noCrmTrace(result)                    # 2.4
  ASSERT reusesAwsClient(result)                                          # 2.6
  ASSERT callExceedsBudget(input) IMPLIES degradesWithinOneSecond(result) # 2.7, 2.8
  ASSERT traceStreamedLive(result) AND loopStaysResponsive(result)        # 2.9, 2.10
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed system produces the same result as the original system.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT processMessage_original(input) == processMessage_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing (Hypothesis) is recommended for preservation because it generates many inputs across the domain, catches edge cases manual tests miss, and gives strong assurance that non-buggy behavior is unchanged. Observe behavior on UNFIXED code first, then write properties capturing it.

**Test Cases**:
1. **Routing preservation**: for generated messages, the fixed `Orchestrator.route` returns the same `agent`/`routing_reason` as before, including the four exact fallback reasons (3.1).
2. **Contract rejection**: a contract-violating primary/chained/CRM response still yields no text and no Trace_Entry (3.2).
3. **Trace_Entry shape**: every emitted entry keeps `agent_selected`, `routing_reason`, a `tools_used` list, and a non-negative integer `time_ms` measuring only that step (3.4).
4. **WebSocket protocol**: `{"text":...}` frames accepted, empty/malformed frames ignored without dropping the socket, incremental `trace` events plus one `response` event still sent in a consumable form (3.5).
5. **History accounting**: a reply appends exactly one user and one assistant turn; the background CRM appends none (3.6).
6. **Recommendation flow**: a recommendation still produces two entries and one NEW lead with the required fields, shown regardless of arrival order (3.8).
7. **Non-License_Advisor routing**: direct routes (including CRM) run as primary with no auto-fire (3.9).
8. **`/invocations`**: still HTTP 200 with reply + complete trace including the CRM entry when invoked; error still returns a safe 200 (3.10).
9. **Health/upload**: `/ping`, `/health`, `/upload-document` unchanged (3.11).
10. **Per-agent rules**: License_Advisor packages/costs/149-word cap/clarifying/custom-quote text unchanged (3.12).
11. **Credentials**: SSO locally, execution role deployed, none in source (3.13).

### Unit Tests

- `process_message` timing fields present and total excludes background CRM.
- `crm_auto_fire` fires only on a recommendation (package pick and custom quote), and is a no-op for greeting/clarifying/fallback/contract-rejected replies.
- CRM confirmation absent from reply; CRM Trace_Entry carries the lead id.
- Cached-client factories construct once and are reused; DynamoDB write path uses the thread-safe strategy.
- `timeout_s` applied in `_bedrock_invoke`; License_Advisor falls back within ~1 s of 10 s; routing falls back within ~1 s of 30 s with `REASON_TIMEOUT`.
- Compliance_Screener still returns within 5 s and falls back to non-model parsing on parse timeout.

### Property-Based Tests

- Generate messages/routing outcomes; assert non-buggy inputs give byte-identical `message`/`trace` before vs after (preservation).
- Generate License_Advisor replies (recommendations vs non-recommendations); assert the CRM fires iff the reply is a recommendation.
- Generate concurrent-call scenarios; assert the reused clients remain safe (no shared non-thread-safe resource across threads) and the client factory is invoked once.

### Integration Tests

- Full WebSocket turn: reply arrives before the background CRM trace; the CRM trace arrives later and is reconciled via the per-turn id; a CRM failure does not close the socket or fail the reply.
- Lead still recorded when the client disconnects immediately after the reply.
- Concurrency: a slow turn on one connection does not block a `/ping` / `/health` probe or a second connection's turn.
- `/invocations` waits for CRM and returns the complete trace including the CRM entry.
