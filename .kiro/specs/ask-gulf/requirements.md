# Requirements Document

## Introduction

Ask Gulf is a 12-agent agentic AI demonstration built for AWS Summit Dubai, presenting a fictional GCC free-zone business setup authority. This spec covers a defined subset of that system: an Orchestrator routing brain, five specialist agents (License Advisor, Application Intake, Compliance Screener, CRM Agent, Appointment Scheduler), a Next.js split-panel frontend, Terraform-based infrastructure automation, a local-first development workflow, and deployment to Amazon Bedrock AgentCore Runtime.

The defining differentiator is a split-panel user interface: a chat panel on the left and a live orchestration trace panel on the right that lets an audience "see the AI thinking" in real time, including which agent was selected, why, which tools and data stores were used, and how long each step took.

The remaining six agents (Document Verifier, Fee Calculator, Knowledge Agent, Status Tracker, Renewal Agent, Feedback Agent) are built later by other teammates and dropped into the same codebase. The agent registry and code structure defined here MUST allow those agents to be added by creating an agent module and a registry entry, with no changes to the Orchestrator.

This document intentionally deviates from the AskGulf Developer Guide in the following areas, per explicit user decisions, and these deviations take precedence over the guide:

- Deployment target is Amazon Bedrock AgentCore Runtime, not EC2/ECS with `.env` keys.
- Local development authenticates via AWS SSO, not static access keys.
- Infrastructure is provisioned via Terraform.
- The Orchestrator and the five in-scope agents run together in a single AgentCore runtime container for now.
- AgentCore Memory may be used for conversation and session state where useful, instead of relying solely on in-memory session dictionaries.

## Glossary

- **Ask_Gulf_System**: The complete application comprising the frontend, agent runtime, and AWS data and AI services.
- **Orchestrator**: The routing component that receives every user message first, selects a specialist agent, delegates the message, and logs the routing decision. It is a router, not a responder.
- **Agent**: A specialist component with a system prompt that receives a user message and conversation history, calls the AI model, and returns a structured response.
- **Specialist_Agent**: Any of the five in-scope agents: License Advisor, Application Intake, Compliance Screener, CRM Agent, Appointment Scheduler.
- **License_Advisor**: The Specialist_Agent that recommends one of four license packages.
- **Application_Intake**: The Specialist_Agent that collects application fields one at a time and saves a draft application.
- **Compliance_Screener**: The Specialist_Agent that screens a name and identifier against a mock watchlist.
- **CRM_Agent**: The Specialist_Agent that creates a lead record, auto-triggered after the License Advisor.
- **Appointment_Scheduler**: The Specialist_Agent that books a meeting slot with a relationship manager.
- **Agent_Registry**: A dictionary mapping each agent name to its metadata, including a short description and a handler reference, used by the Orchestrator for routing.
- **Agent_Description**: A text description of under fifteen words that summarizes an agent's purpose, read by the Orchestrator to make routing decisions.
- **Bedrock**: Amazon Bedrock, providing the Claude models used by the Orchestrator and agents.
- **Sonnet**: The Claude 3.5 Sonnet model used by the Orchestrator for routing.
- **Haiku**: The Claude 3.5 Haiku model that Specialist_Agents may use for faster responses.
- **Trace_Panel**: The right-hand UI panel that displays orchestration trace entries in real time.
- **Trace_Entry**: A record of one agent invocation containing the selected agent, routing reason, tools used, and step latency in milliseconds.
- **Chat_Panel**: The left-hand UI panel where the user exchanges messages with the Ask_Gulf_System.
- **WebSocket_Connection**: The persistent connection between the frontend and the agent runtime used for real-time messaging and trace updates.
- **Agent_Chain**: A sequence in which one agent's response causes the runtime to automatically invoke a subsequent agent.
- **Fallback_Response**: A pre-defined hardcoded response returned by an agent when the AI model errors or times out.
- **Watchlist**: Mock sanctions and politically-exposed-person data stored in `watchlist.json`.
- **Calendar**: Mock appointment slot data stored in `calendar.json`.
- **DynamoDB**: Amazon DynamoDB, the data store for leads and draft applications.
- **S3**: Amazon S3, the object store used for mock data and artifacts.
- **AgentCore_Runtime**: Amazon Bedrock AgentCore Runtime, the managed deployment target.
- **AgentCore_Memory**: The AgentCore feature used for conversation and session state.
- **AWS_SSO**: AWS IAM Identity Center single sign-on, used for local development credentials.
- **Terraform**: The infrastructure-as-code tool used to provision AWS resources.
- **Confidential_Names**: The set of names that MUST never appear in any UI or response: Innovation City, Skynet, Ask Sky, EMB Global, Mantarav.
- **Relationship_Manager**: A staff member with whom applicants can book appointments.

## Requirements

### Requirement 1: Orchestrator Message Routing

**User Story:** As a demo attendee, I want every message routed to the right specialist agent automatically, so that I get relevant answers without choosing an agent myself.

#### Acceptance Criteria

1. WHEN a user message is received, THE Orchestrator SHALL receive the message before any Specialist_Agent processes it.
2. WHEN the Orchestrator processes a user message, THE Orchestrator SHALL use Bedrock with the Sonnet model to select one Specialist_Agent by reading the Agent_Description values from the Agent_Registry, returning a JSON object containing an "agent" field holding the selected Specialist_Agent name and a "reason" field holding a single-sentence routing justification of no more than 200 characters.
3. THE Orchestrator SHALL select the handling agent based on model reasoning over Agent_Description values rather than keyword matching or conditional branching logic.
4. WHEN the Orchestrator has selected a Specialist_Agent, THE Orchestrator SHALL complete the Bedrock selection within 30 seconds and pass the user message and the conversation history to the selected Specialist_Agent.
5. IF the Bedrock selection does not complete within 30 seconds, THEN THE Orchestrator SHALL route the message to the default Specialist_Agent, record the fallback in the Trace_Entry, and continue processing the message without failing it.
6. IF the Bedrock output is malformed or is not valid JSON, THEN THE Orchestrator SHALL route the message to the default Specialist_Agent, record a routing reason indicating malformed selection output, and continue processing the message without failing it.
7. IF the "agent" field contains a name that is not present in the Agent_Registry, THEN THE Orchestrator SHALL route the message to the default Specialist_Agent, record a routing reason indicating an unknown agent name, and continue processing the message without failing it.
8. IF the Bedrock selection produces no confident match, THEN THE Orchestrator SHALL route the message to the default Specialist_Agent, record a routing reason indicating no confident match, and continue processing the message without failing it.
9. WHEN a Specialist_Agent returns a response, THE Orchestrator SHALL return that response to the frontend.
10. THE Orchestrator SHALL return the Specialist_Agent response without generating its own answer to the user.
11. WHEN the Orchestrator completes a routing decision, THE Orchestrator SHALL log a Trace_Entry containing the selected agent name, the routing reason, the tools used, and the elapsed time in milliseconds.

### Requirement 2: Agent Registry and Extensibility

**User Story:** As a developer adding the remaining six agents later, I want to register a new agent without touching the Orchestrator, so that the team can extend the system without refactoring.

#### Acceptance Criteria

1. THE Agent_Registry SHALL map each unique agent name to a record containing exactly one Agent_Description and one handler reference.
2. WHEN the agents package initializer is loaded, THE Ask_Gulf_System SHALL register every available agent into the Agent_Registry.
3. THE Ask_Gulf_System SHALL constrain each Agent_Description to a maximum of fourteen words and a minimum of one word.
4. WHERE a new agent module and a corresponding Agent_Registry entry are added, THE Ask_Gulf_System SHALL route to the new agent without any change to the Orchestrator implementation.
5. WHEN the Orchestrator makes a routing decision, THE Orchestrator SHALL consider only agents present in the Agent_Registry at the time of the decision.
6. IF an agent module fails to import during initialization, THEN THE Ask_Gulf_System SHALL exclude that agent from the Agent_Registry, retain all successfully imported agents in the Agent_Registry, and produce an error indication identifying the failed agent name.
7. IF an agent name is registered more than once, THEN THE Ask_Gulf_System SHALL reject the duplicate registration, retain the first registered record for that name, and produce an error indication identifying the duplicate agent name.

### Requirement 3: Specialist Agent Response Contract

**User Story:** As a developer, I want every agent to follow one response contract, so that the runtime and trace panel handle all agents uniformly.

#### Acceptance Criteria

1. THE Ask_Gulf_System SHALL define each Specialist_Agent with a system prompt.
2. WHEN a Specialist_Agent is invoked, THE Specialist_Agent SHALL receive the user message and the conversation history.
3. WHEN a Specialist_Agent completes processing, THE Specialist_Agent SHALL return a response containing a response text field that is a non-empty string, a tools_used field that is a list of AWS service identifiers, and a time_ms field that is a non-negative integer.
4. WHEN a Specialist_Agent completes processing, THE Specialist_Agent SHALL set time_ms to the elapsed duration in milliseconds measured from invocation start to completion.
5. WHERE a Specialist_Agent produces structured output, THE Specialist_Agent SHALL include that output as structured data in the response.
6. WHERE a Specialist_Agent is configured for faster responses, THE Specialist_Agent SHALL use the Haiku model.
7. IF a Specialist_Agent produces a response that violates the response contract, THEN THE Ask_Gulf_System SHALL reject the response, retain the conversation history unchanged, and return an error indication identifying the contract violation to the caller.

### Requirement 4: License Advisor Agent

**User Story:** As a prospective business owner, I want a license package recommendation, so that I know which option fits my needs and its cost.

#### Acceptance Criteria

1. IF a request is missing one or more of the three required attributes (business activity, team size, timeline), THEN THE License_Advisor SHALL ask between one and two clarifying questions targeting only the missing attributes.
2. WHEN the License_Advisor has all three required attributes, THE License_Advisor SHALL recommend exactly one package from the set: Idea at AED 6,600 per year for a solo founder, Seed at AED 12,500 per year for one to two visas, Startup at AED 22,000 per year for three visas with flex office, and Growth at AED 45,000 per year for ten visas with premium office.
3. WHEN the License_Advisor recommends a package, THE License_Advisor SHALL include the annual cost in AED and a reason for the recommendation.
4. THE License_Advisor SHALL limit each recommendation response to no more than 149 words.
5. IF the stated requirements exceed the Growth tier, THEN THE License_Advisor SHALL offer a custom quote instead of recommending a fixed package.
6. IF the model service fails or does not respond within 10 seconds, THEN THE License_Advisor SHALL return a predefined Fallback_Response, retain all known user attributes, and include an indication that a fallback response was provided.

### Requirement 5: Application Intake Agent

**User Story:** As an applicant, I want to be guided through an application one question at a time, so that I can complete it without confusion, and I want a draft saved.

#### Acceptance Criteria

1. WHEN the Application_Intake begins collecting an application, THE Application_Intake SHALL request one field at a time in the order: company name, business activity, package type, number of shareholders, primary shareholder nationality, contact email.
2. WHEN the user provides a valid value for the currently requested field, THE Application_Intake SHALL store the value and request the next uncollected field in the defined order.
3. IF the user provides a value that fails validation for the currently requested field, THEN THE Application_Intake SHALL reject the value, retain all previously collected field values, display a message indicating which field is invalid and the expected format, and re-request the same field.
4. WHERE the user provides values for multiple fields in a single response, THE Application_Intake SHALL accept only the value corresponding to the currently requested field, ignore any additional values, and continue requesting remaining uncollected fields one at a time in the defined order.
5. IF the value provided for number of shareholders is not an integer between 1 and 50 inclusive, THEN THE Application_Intake SHALL reject the value, retain all previously collected field values, display a message indicating the expected range, and re-request the field.
6. IF the value provided for contact email does not contain a single "@" separating a non-empty local part and a domain part containing at least one ".", or exceeds 254 characters, THEN THE Application_Intake SHALL reject the value, retain all previously collected field values, display a message indicating the expected email format, and re-request the field.
7. WHEN all six fields are collected, THE Application_Intake SHALL present a summary listing each field label and its collected value to the user.
8. WHEN all six fields are collected, THE Application_Intake SHALL save a draft application record to the DynamoDB table ask-gulf-leads containing an application identifier in the format APP-2026-XXXX where XXXX is a four-digit sequence from 0000 to 9999, a status of DRAFT, the six collected field values, and a creation timestamp in ISO 8601 UTC format.
9. IF saving the draft application record to DynamoDB fails, THEN THE Application_Intake SHALL retain the collected field values, display a message indicating that the draft could not be saved, and not report successful submission to the user.
10. WHEN a draft application record is successfully saved, THE Application_Intake SHALL inform the user that a relationship manager will make contact within 24 hours.

### Requirement 6: Compliance Screener Agent

**User Story:** As a compliance officer, I want a shareholder screened against sanctions and PEP lists with an evidence trail, so that I can decide whether to proceed.

#### Acceptance Criteria

1. WHEN the Compliance_Screener receives a message, THE Compliance_Screener SHALL parse a name and an identifier from the message.
2. IF a message does not contain a parseable name or a parseable identifier, THEN THE Compliance_Screener SHALL return a request asking the user to provide the missing name or identifier, and SHALL NOT terminate with a failure state.
3. WHEN a name and identifier are parsed, THE Compliance_Screener SHALL check the name and identifier against the Watchlist using a case-insensitive exact match, where a match occurs if the name exactly matches, ignoring case, a Watchlist entry name OR any provided identifier exactly matches, ignoring case, any identifier in that Watchlist entry identifiers list.
4. WHEN a screening completes, THE Compliance_Screener SHALL return, within 5 seconds of receiving a parseable name and identifier, a structured result containing the screening result label, the name, the identifier, the status, the lists checked, a timestamp, and the evidence.
5. THE Compliance_Screener SHALL report the lists checked as OFAC SDN, UN Consolidated, and EU Sanctions.
6. IF the screened entity matches exactly one Watchlist entry, THEN THE Compliance_Screener SHALL set the status to flagged, state that the entity requires manual review, and include evidence identifying the matching list and the reason for the match.
7. IF the screened entity matches more than one Watchlist entry, THEN THE Compliance_Screener SHALL set the status to flagged, state that the entity requires manual review, and include evidence identifying all matching lists and the reason for each match.
8. IF the screened entity does not match any Watchlist entry, THEN THE Compliance_Screener SHALL set the status to clear, state that the entity is cleared to proceed, and include evidence stating that no matches were found across the lists checked.

### Requirement 7: CRM Agent and Automatic Lead Capture

**User Story:** As a sales manager, I want a lead automatically captured after a licensing conversation, so that no prospect is lost and I can see it happen in the trace.

#### Acceptance Criteria

1. WHEN the License_Advisor completes a response, THE Ask_Gulf_System SHALL invoke the CRM_Agent in the background for the same user message.
2. WHEN the CRM_Agent is invoked, THE CRM_Agent SHALL create a lead record in DynamoDB containing a lead identifier in the format LEAD-<timestamp>, a source of aws-summit-demo, a status of NEW, an interaction summary, and a creation timestamp.
3. WHEN the CRM_Agent creates a lead record, THE CRM_Agent SHALL use the DynamoDB tool.
4. WHEN the CRM_Agent completes, THE CRM_Agent SHALL return a confirmation and the lead record as structured data.
5. WHEN both the License_Advisor and the CRM_Agent complete for one user message, THE Ask_Gulf_System SHALL produce two Trace_Entry records for that message.

### Requirement 8: Appointment Scheduler Agent

**User Story:** As an applicant, I want to book a meeting with a relationship manager, so that I can move forward with my setup.

#### Acceptance Criteria

1. WHEN the Appointment_Scheduler is invoked, THE Appointment_Scheduler SHALL read available slots from the Calendar, where each slot contains a slot identifier, a date, a time, a relationship manager name, and a booked flag.
2. WHEN the Appointment_Scheduler books a slot, THE Appointment_Scheduler SHALL select a slot whose booked flag is not set.
3. WHEN a slot is booked, THE Appointment_Scheduler SHALL return the confirmed slot details and a calendar event.

### Requirement 9: Multi-Agent Chaining

**User Story:** As a demo presenter, I want one message to flow through several agents automatically, so that the audience sees an end-to-end journey in a single turn.

#### Acceptance Criteria

1. WHERE an agent response carries a chaining flag, THE Ask_Gulf_System SHALL automatically invoke the next agent indicated by the flag.
2. WHEN a chained agent is invoked, THE Ask_Gulf_System SHALL append the chained agent response to the prior response and append a corresponding Trace_Entry.
3. WHEN a message triggers the Fee Calculator, Application_Intake, and Appointment_Scheduler chain, THE Ask_Gulf_System SHALL produce three Trace_Entry records for that single message.
4. IF an agent referenced in an Agent_Chain is not present in the Agent_Registry, THEN THE Ask_Gulf_System SHALL continue the interaction with the available agents and SHALL NOT fail the message.

### Requirement 10: Fallback Resilience

**User Story:** As a demo presenter, I want the system to keep responding even if the AI model fails, so that the demo never stalls in front of an audience.

#### Acceptance Criteria

1. IF a Bedrock call errors or times out during an agent invocation, THEN THE affected Specialist_Agent SHALL return a pre-defined Fallback_Response for that agent.
2. WHEN a Fallback_Response is returned, THE Specialist_Agent SHALL set the tools used to the fallback-mode value.
3. THE Ask_Gulf_System SHALL define a Fallback_Response for each Specialist_Agent.

### Requirement 11: Real-Time WebSocket Trace

**User Story:** As a demo attendee, I want to watch the orchestration trace update live as agents work, so that I can see the AI reasoning as it happens.

#### Acceptance Criteria

1. THE frontend SHALL communicate with the agent runtime over a WebSocket_Connection.
2. WHEN a Trace_Entry is produced, THE Ask_Gulf_System SHALL send the Trace_Entry to the frontend over the WebSocket_Connection in real time.
3. WHEN the frontend receives a Trace_Entry, THE Trace_Panel SHALL display the selected agent, the routing reason, the tools and data stores used, and the step latency.
4. WHEN multiple Trace_Entry records are produced for one message, THE Trace_Panel SHALL display each Trace_Entry.

### Requirement 12: Split-Panel Frontend UI and Theme

**User Story:** As a demo attendee, I want a clear split-panel interface with consistent branding, so that the chat and the AI reasoning are both easy to follow.

#### Acceptance Criteria

1. THE frontend SHALL present a split-panel layout with the Chat_Panel occupying the left seventy percent and the Trace_Panel occupying the right thirty percent.
2. THE frontend SHALL display the Ask Gulf branding in the header.
3. THE Trace_Panel SHALL display AWS service badges for Bedrock, DynamoDB, and S3.
4. THE frontend SHALL apply the GulfKloud theme colors: navy #0A1628, mid #0D1F35, accent blue #2196F3, white text #FFFFFF, secondary #A0AEC0, success green #27AE60, warning orange #F5A623, error red #E74C3C, and card #1A2D42.

### Requirement 13: Confidential Name Exclusion

**User Story:** As the demo owner, I want certain confidential names to never appear, so that the demo does not disclose protected information.

#### Acceptance Criteria

1. THE Ask_Gulf_System SHALL exclude all Confidential_Names from every UI element.
2. THE Ask_Gulf_System SHALL exclude all Confidential_Names from every agent response.

### Requirement 14: Mock Data and Data Stores

**User Story:** As a developer, I want mock data files and provisioned data stores, so that the agents have realistic data to operate on during the demo.

#### Acceptance Criteria

1. THE Ask_Gulf_System SHALL provide mock data files including `watchlist.json`, `calendar.json`, `applications.json`, and `fee_schedule.json`.
2. THE Compliance_Screener SHALL read screening data from `watchlist.json`, where each flagged entry contains a name, identifiers, a list, and a reason.
3. THE Appointment_Scheduler SHALL read slot data from `calendar.json`.
4. THE Ask_Gulf_System SHALL provide a DynamoDB table named ask-gulf-leads for lead records.
5. THE Ask_Gulf_System SHALL persist draft application records to DynamoDB.
6. WHERE mock data is loaded into AWS data stores, THE Ask_Gulf_System SHALL load the relevant mock data into DynamoDB and S3.

### Requirement 15: Performance

**User Story:** As a demo attendee, I want responses to arrive quickly, so that the demo feels responsive on stage.

#### Acceptance Criteria

1. WHEN a user message is submitted over a good network connection, THE Ask_Gulf_System SHALL return a response in under three seconds.

### Requirement 16: Credential Security and AWS SSO Local Development

**User Story:** As a developer, I want secure credential handling with SSO locally and roles in the cloud, so that no secrets are committed or hardcoded.

#### Acceptance Criteria

1. THE Ask_Gulf_System SHALL NOT contain hardcoded AWS credentials in any source file.
2. WHERE the Ask_Gulf_System runs in local development, THE Ask_Gulf_System SHALL obtain AWS credentials through AWS_SSO.
3. WHERE the Ask_Gulf_System runs in the deployed environment, THE Ask_Gulf_System SHALL obtain AWS credentials through IAM roles or the AgentCore execution role.
4. THE Ask_Gulf_System SHALL exclude secrets from version control.

### Requirement 17: Terraform Infrastructure Automation

**User Story:** As a developer, I want infrastructure provisioned as code, so that the environment is reproducible and reviewable.

#### Acceptance Criteria

1. THE Terraform configuration SHALL provision the DynamoDB table or tables used by the Ask_Gulf_System.
2. THE Terraform configuration SHALL provision the S3 bucket used by the Ask_Gulf_System.
3. THE Terraform configuration SHALL provision the IAM roles and policies used by the Ask_Gulf_System.
4. THE Terraform configuration SHALL provision the AgentCore_Runtime deployment resources.

### Requirement 18: Local-First Run and AgentCore Runtime Deployment

**User Story:** As a developer, I want to run everything locally first and then deploy the same code to AgentCore, so that I can iterate quickly and ship confidently.

#### Acceptance Criteria

1. THE Ask_Gulf_System SHALL support running the agent runtime and frontend locally before deployment.
2. THE Ask_Gulf_System SHALL deploy the Orchestrator and the five in-scope Specialist_Agents together in a single AgentCore_Runtime container.
3. WHERE conversation or session state is needed, THE Ask_Gulf_System SHALL use AgentCore_Memory to hold that state.

### Requirement 19: Demo Scenario Support

**User Story:** As a demo presenter, I want the system to support the scripted scenarios end to end, so that the on-stage narrative works as rehearsed.

#### Acceptance Criteria

1. WHEN a user submits a message expressing intent to set up a company, THE Ask_Gulf_System SHALL route to the License_Advisor, return a package recommendation with cost, auto-fire the CRM_Agent in the background, and display two Trace_Entry records.
2. WHEN a user submits a shareholder screening request containing a name and passport identifier, THE Ask_Gulf_System SHALL route to the Compliance_Screener and return a structured screening result with an evidence trail.
3. WHEN a user submits a message indicating readiness to proceed with a package, shareholder count, and visa count, THE Ask_Gulf_System SHALL route to the Fee Calculator, chain to the Application_Intake, chain to the Appointment_Scheduler, and display three Trace_Entry records for that single message.
