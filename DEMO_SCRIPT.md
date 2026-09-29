# Ask Gulf - Demo Script

A verified, end-to-end walkthrough for the demo video. Every prompt below has
been tested against the running app and behaves predictably. Follow the wording
exactly on camera - improvised phrasing can make the LLM ask a clarifying
question instead of doing what you expect.

---

## Pre-flight (do this BEFORE recording)

1. Refresh AWS credentials (the #1 cause of a broken demo):
   ```
   aws sso login --profile default
   ```
2. Make sure the stack is up and healthy:
   ```
   docker compose up -d
   curl.exe -s http://localhost:8080/health
   ```
   Expect: `{"status":"ok","deployment_mode":"local","registered_agents":5}`
3. Open the UI: http://localhost:3000
4. Send ONE throwaway prompt (e.g. "hello") to warm up the Bedrock client, then
   refresh the page for a clean start. This avoids a slow/cold first response on
   camera.

TELL-TALE SIGN OF EXPIRED CREDENTIALS: every reply becomes the same
"Our licensing options range from the Idea package..." fallback text. If you see
that, stop, run `aws sso login --profile default`, and continue (no rebuild needed).

Keep the ORCHESTRATION TRACE panel visible the whole time - it is the proof that
this is a real multi-agent system (routing decisions, per-agent latency, AWS
service badges: Bedrock / DynamoDB / S3).

---

## Segment 1 - License Advisor + CRM (headline: context-aware conversation)

Send each line as a SEPARATE message. Keep the business activity consistent
(fintech) throughout - do not switch activities mid-conversation.

```
hello
```
```
i want to start a fintech company
```
```
we'll have 3 people
```
```
launching next month
```

NARRATE:
- One conversation, and each short answer ("fintech", "3 people", "next month")
  STAYS with the License Advisor and builds toward a single recommendation.
- Point at the trace: the routing reason literally says it is "continuing the
  license recommendation conversation" - the assistant holds context instead of
  re-routing every fragment. (This was a real bug we fixed.)
- Final recommendation: Startup package, AED 22,000/year, 3 visas.
- A SECOND card appears after the reply: crm_agent auto-fires and captures the
  lead in DynamoDB (watch the DynamoDB badge).

---

## Segment 2 - Application Intake (structured multi-turn form)

Send the first line, then answer each question in turn:

```
I want to start my business setup application
```
```
Gulf Tech LLC
```
```
software development
```
```
Startup
```
```
3
```
```
Indian
```
```
founder@gulftech.example
```

NARRATE:
- The assistant collects one field at a time, validates input, and finishes by
  saving a DRAFT application.
- The trace stays on `application_intake` for every turn - a nice contrast to
  the topic-switching in the next segment.

OPTIONAL (show validation): answer the shareholders question with `100` first.
It is out of range, so the agent rejects it and re-asks before you give `3`.

---

## Segment 3 - Compliance Screener (flagged + clear)

```
Run a compliance screening on John Doe, passport P1234567
```
NARRATE: routes to compliance_screener, returns FLAGGED against OFAC SDN with
evidence and a timestamp. Deterministic, auditable check (S3 badge = watchlist read).

```
Screen Jane Smith, ID Z1112223
```
NARRATE: same agent, CLEAR - no watchlist match. Shows both outcomes.

OPTIONAL (richer matching): 
```
Screen Chen Wei, passport Q1231231
```
Flags on the NAME even though the passport does not match - the screener matches
on either name or identifier against the 12-entry watchlist.

---

## Segment 4 - Appointment Scheduler

```
Book me a meeting with a relationship manager
```
NARRATE: routes to appointment_scheduler, books the first open slot
(Layla Hassan, 2026-01-20, 10:00) with a confirmation reference. Note the ~30ms
latency - deterministic, no LLM call needed.

---

## Segment 5 - Direct CRM (standalone lead capture)

```
Please capture my details as a lead
```
NARRATE: routes directly to crm_agent (not just the auto-fire), writes a real
lead with a LEAD-<timestamp> ID to DynamoDB.

---

## Closer - automated end-to-end proof (optional, strong finish)

Drop to a terminal and run the automated suite:
```
python e2e_test.py
```
NARRATE: this exercises ALL FIVE agents plus context-aware routing, CRM writes,
and the transport layer automatically - 33 checks, all green. Proof the whole
system works end to end, not just the parts clicked live.

---

## The five agents (checklist for full coverage)

1. License Advisor       - Segment 1 (recommendation)
2. CRM Agent             - Segment 1 (auto-fire) + Segment 5 (direct)
3. Application Intake    - Segment 2
4. Compliance Screener   - Segment 3
5. Appointment Scheduler - Segment 4

---

## Narration themes to emphasize (what a manager cares about)

- Multi-agent orchestration: one Orchestrator routes each message to the right
  specialist by reasoning over agent descriptions - no hardcoded keyword rules.
  The trace panel makes every routing decision visible.
- Context-aware routing: the assistant continues an in-progress conversation
  instead of misrouting short follow-ups (Segment 1).
- Real AWS integration: Bedrock (Claude) for reasoning, DynamoDB for leads and
  applications, S3 for documents/reference data - shown live via the badges.
- Resilience: enforced per-call timeouts and graceful fallbacks mean a single
  slow or failed AI call never crashes the app - it degrades safely.
- Observability: live streamed trace with routing time, total turn time, and
  per-step latency.