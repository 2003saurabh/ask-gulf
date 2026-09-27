# Bugfix Requirements Document

## Introduction

When Ask Gulf runs locally against real Bedrock and DynamoDB, replies are slow and the trace doesn't behave the way the Developer Guide describes. For one observed message, the Trace_Panel showed `license_advisor` at 1825 ms and `crm_agent` at 225 ms, and the chat reply ended with "Lead LEAD-1790414525 captured. A relationship manager will follow up shortly." That lead line gets added even to a greeting like "hello".

Six defects cause this:

- Routing time and total turn time are never measured, so the trace under-reports how long the user waited.
- The CRM auto-fire runs in the foreground, so its time is added to every License_Advisor reply.
- The CRM fires on every License_Advisor turn (greetings, clarifying questions, fallbacks). Each time it writes a lead and adds its confirmation to the chat reply.
- AWS client setup is repeated on every Bedrock, DynamoDB, and S3 call.
- The 10-second agent budget and the 30-second routing budget are not enforced, so a slow Bedrock call can hold up a turn for a minute or more.
- Trace events are held back until the turn finishes, and the server handles nothing else while a turn is running.

Impact:

- Every turn carries avoidable latency against the "under 3 seconds on a good connection" target (Guide §9.4, R15.1).
- The trace doesn't update in real time (Guide §1.1, R11.2).
- The demo can stall well past its fallback budgets when Bedrock is slow at the venue (Guide §9.3).
- Spurious leads are written to DynamoDB.

Scope: the AWS Summit demo is on 30 September 2026, so the fix must be contained and low-risk. These are out of scope:

- The six agents that aren't built yet.
- Moving routing from Sonnet to Haiku (a possible follow-up).
- Region and model-ID defaults.

The fix removes avoidable overhead and shows the true total time. It can't guarantee under 3 seconds if the routing and License_Advisor model calls alone take longer than that.

Behavior change to confirm in review: R7.1 fires the CRM_Agent "WHEN the License_Advisor completes a response". This fix fires it only when the License_Advisor completes a recommendation, matching Guide §4.7 ("triggered automatically when the License Advisor has finished a recommendation"). It also moves the lead confirmation out of the chat reply and into the Trace_Panel, matching Guide Scenario 1.

In this document, a *recommendation* is a License_Advisor reply that does one of two things:

- Recommends exactly one of the four packages with its annual AED cost (R4.2, R4.3).
- Offers a custom quote (R4.5).

If the License_Advisor answers the Scenario 1 opener with clarifying questions, the CRM card will appear on the later turn that makes the recommendation.

References: R#.# is a requirement in `.kiro/specs/ask-gulf/requirements.md`. "Guide" is `AskGulf_Developer_Guide.docx`.

## Bug Analysis

### Current Behavior (Defect)

What happens today:

1.1 WHEN a message is processed THEN the system does not measure or report the time taken by the Orchestrator's routing decision (a Bedrock Sonnet call that runs before any agent), so the trace under-reports how long the user waited

1.2 WHEN a message is processed THEN the system does not measure or report the total time from receiving the message to sending the reply, so a turn cannot be checked against the 3-second target

1.3 WHEN the License_Advisor completes a response to a message received over the WebSocket_Connection THEN the system runs the auto-fired CRM_Agent in the foreground and sends the reply only after the CRM_Agent finishes, which adds the CRM step (225 ms observed) to every License_Advisor reply

1.4 WHEN the License_Advisor's response is not a recommendation THEN the system still auto-fires the CRM_Agent, writes a new lead record to DynamoDB, and adds a CRM Trace_Entry for that message. This happens for a reply to a greeting like "hello" (including when the License_Advisor was picked only as the default agent after a routing fallback), a clarifying question, a reply that mentions packages without recommending one, the License_Advisor's Fallback_Response, and a response rejected by the contract guard

1.5 WHEN the CRM_Agent is auto-fired THEN the system appends the CRM_Agent's confirmation (for example "Lead LEAD-1790414525 captured. A relationship manager will follow up shortly.") to the chat reply

1.6 WHEN a Specialist_Agent calls Bedrock or DynamoDB, or a document is uploaded to S3, THEN the system repeats AWS client setup for that call. It creates a new client (measured locally without network access at 106–167 ms for Bedrock and 139–245 ms for a DynamoDB table), re-resolves credentials (an extra credential request per call when an SSO profile is used), and opens a new TLS connection

1.7 WHEN a Specialist_Agent's Bedrock call is slow or unresponsive THEN the system does not enforce the call's time budget (10 seconds for the License_Advisor, R4.6 and R10.1). It waits for the AWS SDK's default 60-second read timeout, repeated across automatic retries, before degrading. The Compliance_Screener can miss its 5-second result limit (R6.4) the same way

1.8 WHEN the Orchestrator's routing call runs longer than 30 seconds THEN the system keeps waiting until that call finishes before routing to the default agent, so the 30-second routing fallback (R1.5) doesn't return on time

1.9 WHEN a message received over the WebSocket_Connection produces Trace_Entry records THEN the system holds every trace event until the whole turn has finished and sends them together just before the reply, so the Trace_Panel doesn't update as each step completes (R11.2)

1.10 WHEN a message is being processed THEN the system blocks the server for the whole turn, so other WebSocket connections and HTTP requests, including `/health` and the `/ping` health probe, wait until the turn finishes

### Expected Behavior (Correct)

What should happen instead:

2.1 WHEN a message is processed THEN the system SHALL measure the Orchestrator's routing time, including when routing falls back to the default agent, and SHALL report it alongside that message's trace, and the Trace_Panel SHALL display it without adding a Trace_Entry record

2.2 WHEN a message is processed THEN the system SHALL measure the total time from receiving the message to sending the reply (not counting any background CRM step), SHALL log it, and SHALL report it alongside that message's trace, and the Trace_Panel SHALL display it

2.3 WHEN the License_Advisor completes a recommendation for a message received over the WebSocket_Connection THEN the system SHALL send the reply without waiting for the CRM_Agent, SHALL run the CRM_Agent in the background for the same message, and SHALL send the CRM_Agent's Trace_Entry as soon as it completes, even if that is after the reply. The background step SHALL NOT delay the next message. A failure in the background step SHALL NOT fail the reply or close the connection. The system SHALL still record the lead if the client disconnects before the background step finishes

2.4 WHEN the License_Advisor's response is not a recommendation, whether the License_Advisor was picked by the model or as the default agent, THEN the system SHALL NOT invoke the CRM_Agent, SHALL NOT create a lead record, and SHALL NOT produce a CRM_Agent Trace_Entry for that message. This applies to greeting replies, clarifying questions, replies that mention packages without recommending one, the License_Advisor's Fallback_Response, and responses rejected by the contract guard

2.5 WHEN the CRM_Agent is auto-fired THEN the system SHALL keep the CRM_Agent's confirmation out of the reply text and SHALL show the lead capture in the Trace_Panel through the CRM_Agent's Trace_Entry. When the lead was saved, that Trace_Entry SHALL identify the created lead record (for example LEAD-1790414525)

2.6 WHEN a Specialist_Agent calls Bedrock or DynamoDB, or a document is uploaded to S3, THEN the system SHALL reuse the AWS client setup already done in the running process instead of repeating client creation, credential resolution, and connection setup on every call. The reused clients SHALL remain safe when calls run at the same time (for example, a background CRM step running while the next message is processed)

2.7 WHEN a Specialist_Agent's Bedrock call does not complete within its time budget THEN the system SHALL stop waiting no more than 1 second after the budget runs out and SHALL degrade as already specified. The License_Advisor SHALL return its Fallback_Response with tools_used `["fallback-mode"]` (R4.6, R10.1, R10.2), and the Compliance_Screener SHALL still return its screening result or its request for missing input within 5 seconds (R6.4)

2.8 WHEN the Orchestrator's routing call does not complete within 30 seconds THEN the system SHALL stop waiting no more than 1 second after the 30-second budget runs out and SHALL route to the default agent with routing_reason "routing timed out; using default agent"

2.9 WHEN a Trace_Entry is produced for a message received over the WebSocket_Connection THEN the system SHALL send that trace event as soon as its step completes, without waiting for later steps or the reply, and the Trace_Panel SHALL render each card as it arrives

2.10 WHEN a message is being processed THEN the system SHALL keep serving other WebSocket connections and HTTP requests, including `/health` and `/ping`, without waiting for that turn to finish

### Unchanged Behavior (Regression Prevention)

Existing behavior that must stay the same:

3.1 WHEN the Orchestrator routes a message THEN the system SHALL CONTINUE TO pick one agent through Sonnet model reasoning over the registered Agent_Description values. On a routing anomaly it SHALL CONTINUE TO route to the default agent with exactly one of these reasons: "routing timed out; using default agent", "malformed selection output; using default agent", "unknown agent name; using default agent", or "no confident match; using default agent" (R1.2–R1.8)

3.2 WHEN any agent (primary, chained, or auto-fired CRM_Agent) returns a response that violates the response contract THEN the system SHALL CONTINUE TO reject it, so that it adds no reply text and no Trace_Entry (R3.7)

3.3 WHEN a Bedrock call errors, AWS credentials are missing or expired, or the CRM_Agent's DynamoDB write fails THEN the system SHALL CONTINUE TO degrade as it does today without failing the message: the affected agent returns its predefined Fallback_Response with tools_used `["fallback-mode"]`, or the Compliance_Screener still parses the request without the model (R4.6, R10.1–R10.3)

3.4 WHEN a Trace_Entry is produced THEN the system SHALL CONTINUE TO include agent_selected, routing_reason, a tools_used list, and a non-negative integer time_ms that measures only that agent's own step. The primary agent's entry SHALL CONTINUE TO carry the Orchestrator's routing_reason

3.5 WHEN the frontend exchanges messages over `/ws/{session_id}` THEN the system SHALL CONTINUE TO accept `{"text": ...}` frames and ignore empty or malformed frames without dropping the connection. For each message it SHALL CONTINUE TO send incremental `{"type":"trace","entry":...}` events and one `{"type":"response","message":...,"trace":[...]}` event, in a form the existing frontend can consume

3.6 WHEN a message produces reply text THEN the system SHALL CONTINUE TO append exactly one user turn and one assistant turn, containing that reply text, to the session history. The background CRM step SHALL NOT append any turn

3.7 WHEN an agent response carries chain_next THEN the system SHALL CONTINUE TO invoke the named agents in order, append their text to the reply, produce one Trace_Entry per step, stop after five distinct agents or on a repeated agent, and skip an agent missing from the Agent_Registry without failing the message (R9)

3.8 WHEN the License_Advisor completes a recommendation THEN the system SHALL CONTINUE TO produce exactly two Trace_Entry records for that message (License_Advisor, then CRM_Agent). It SHALL CONTINUE TO create one NEW lead with a LEAD-<timestamp> identifier, source aws-summit-demo, an interaction summary, and a creation timestamp (R7.2–R7.5, R19.1). The Trace_Panel SHALL CONTINUE TO display both entries whether the CRM entry arrives before or after the reply

3.9 WHEN a message is routed to an agent other than the License_Advisor, including the CRM_Agent directly, THEN the system SHALL CONTINUE TO run that agent as the primary agent, include its text in the reply, and not auto-fire the CRM_Agent

3.10 WHEN a message is processed through `POST /invocations` THEN the system SHALL CONTINUE TO return HTTP 200 with the reply text and the complete trace for that message, including the CRM_Agent's Trace_Entry whenever the CRM_Agent was invoked. On a processing error it SHALL CONTINUE TO return a safe fallback message with HTTP 200

3.11 WHEN `GET /ping`, `GET /health`, or `POST /upload-document` is called THEN the system SHALL CONTINUE TO return, respectively, `{"status":"Healthy"}`; the runtime status with deployment mode and registered-agent count; and the stored file_key, or an error status that never claims success

3.12 WHEN a Specialist_Agent handles a message THEN the system SHALL CONTINUE TO follow that agent's existing rules:

- License_Advisor: packages, AED costs, the 149-word cap, clarifying questions, and custom quotes.
- Application_Intake: field order, validation, and draft saving.
- Compliance_Screener: matching and evidence.
- Appointment_Scheduler: slot booking.

3.13 WHEN the system obtains AWS credentials THEN it SHALL CONTINUE TO use the AWS SSO profile in local development and the IAM or AgentCore execution role when deployed, with no credentials in source (R16)
