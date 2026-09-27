# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Keep the in-progress agent on a context-dependent follow-up
  - **CRITICAL**: This test MUST FAIL on the unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate stateless routing misroutes an in-context follow-up
  - **Scoped PBT Approach**: The two reproductions are deterministic; scope the property to the concrete REPRO 1 / REPRO 2 cases using an injected fake Sonnet + fake registry (no real AWS, no model stochasticity)
  - Use a fake Bedrock/Sonnet that inspects the routing prompt it receives: if the prompt does NOT include the recent history and the last agent (unfixed), return the stateless misroute (appointment_scheduler / compliance_screener); if it DOES (fixed), return the in-progress agent (license_advisor / application_intake). This ties the assertion to the fix's observable contract (Bug Condition + Correctness Property 1 in design)
  - REPRO 1: history's last assistant turn is license_advisor asking for the timeline, `last_agent="license_advisor"`, user turn "in next month" → assert the routing prompt INCLUDES the recent history and the last agent AND `decision.agent == "license_advisor"` (not appointment_scheduler)
  - REPRO 2: history's last assistant turn is application_intake asking for the company name, `last_agent="application_intake"`, user turn "Gulf Tech LLC" → assert the routing prompt INCLUDES the recent history and the last agent AND `decision.agent == "application_intake"` (not compliance_screener)
  - Topic-change (2.4): when the user clearly changes topic mid-flow, assert routing follows the new-topic agent rather than being forced to stay
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS for REPRO 1 and REPRO 2 (proves the bug: prompt lacks history/last_agent so the fake returns the misroute)
  - Document counterexamples found ("in next month" → appointment_scheduler; "Gulf Tech LLC" → compliance_screener)
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3, 2.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Stateless routing outcomes unchanged
  - **IMPORTANT**: Follow observation-first methodology; reuse/extend the existing §3.1 routing preservation tests in `backend/tests/test_preservation_latency_trace_fix.py`
  - Observe on UNFIXED code: confident selection routes to the chosen agent with the ≤200-char stripped reason; the four fallback reasons are byte-for-byte exact (timeout, malformed/Bedrock error, unknown name, no-match)
  - Add a preservation test asserting `route(message, [])` called positionally (no `last_agent`) yields an unchanged `RoutingDecision`, and that with empty history and no last_agent the routing prompt path is unchanged from today
  - Property-based testing generates many cases for stronger guarantees across the input domain
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 3. Fix for context-free routing (thread recent history + last_agent into the routing prompt)

  - [x] 3.1 Implement the fix in `backend/orchestrator.py`
    - Change `route` signature to `route(self, user_message, history, last_agent=None)` (new DEFAULTED parameter; positional `route(message, history)` stays working)
    - Add `_format_recent_context(history, last_agent)` rendering the last few turns as `role: content` lines plus a line naming the last-selected agent; return `""` when there is no history AND no last_agent so the prompt is byte-for-byte identical to today (preserves first-turn routing)
    - Thread the recent context into `_build_routing_prompt(candidates, recent_context)`; when non-empty, append the recent conversation, the last-agent line, and the routing instruction: if the previous assistant turn was an agent collecting information (a clarifying question or an application-field request), route this turn to that SAME agent unless the user clearly changed topic
    - Keep routing purely LLM-driven (no keyword/if-else selection); keep `_invoke_sonnet`'s module-level daemon executor and `future.result(timeout=ROUTING_TIMEOUT_S)` unchanged (no `with ThreadPoolExecutor()` block)
    - Update docstrings (history is now used; document `last_agent`)
    - _Bug_Condition: isBugCondition((user_message, history, last_agent)) from design_
    - _Expected_Behavior: route' keeps collectingAgent(history) for buggy inputs; unchanged decision otherwise_
    - _Preservation: Preservation Requirements from design (four fallbacks, ≤200-char reason, RoutingDecision shape, C4 budget)_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4, 3.5, 3.7_

  - [x] 3.2 Wire last_agent memory into `backend/main.py`
    - Add a process-wide, thread-safe `session_id -> last_agent` store (dict + lock, mirroring `_s3_client_singleton`) with a `reset_last_agent_cache()` test seam
    - In `run_foreground`, read the remembered `last_agent` before routing and call `orch.route(user_message, history, last_agent=last_agent)`; after the decision, record `decision.agent` for the session
    - Leave the session-store `{role, content}` contract and all latency-trace-fix behaviors untouched (C1/C2/C3/C5/C6)
    - _Preservation: 3.6 (runtime behavior unchanged), session-store contract unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 3.6_

  - [x] 3.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Keep the in-progress agent on a context-dependent follow-up
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - Run the bug condition exploration test from step 1
    - **EXPECTED OUTCOME**: Test PASSES (REPRO 1 → license_advisor, REPRO 2 → application_intake; prompt now includes history + last_agent)
    - _Requirements: 2.1, 2.2, 2.3, 2.4_

  - [x] 3.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Stateless routing outcomes unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from step 2 plus the existing §3.1 routing preservation tests
    - **EXPECTED OUTCOME**: Tests PASS (no regressions; four fallbacks, ≤200-char reason, positional call, C4 budget all unchanged)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 4. Checkpoint - Ensure the full backend suite is green
  - Run `.\.venv\Scripts\python.exe -m pytest tests/ -q` from `backend/`
  - Ensure all tests pass (existing 48 + the new exploration/preservation tests); ask the user if questions arise
