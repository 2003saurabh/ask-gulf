"""Per-agent fallback responses for Ask Gulf.

Every Specialist_Agent has a concrete, predefined Fallback_Response returned
when the AI model errors or times out (R10.1, R10.3). The runtime uses these
strings via the shared Bedrock helper, which sets ``tools_used=["fallback-mode"]``
when a fallback is served (R10.2).

The strings below are copied verbatim from the design document, §4
("The Five In-Scope Agents"). Keys are the canonical agent names used in the
Agent_Registry.
"""

from __future__ import annotations

FALLBACK_RESPONSES: dict[str, str] = {
    # §4.1 License Advisor (R4.6, 10s budget)
    "license_advisor": (
        "Our licensing options range from the Idea package at AED 6,600/year "
        "for solo founders to the Growth package at AED 45,000/year for larger "
        "teams. A relationship manager can walk you through the best fit."
    ),
    # §4.2 Application Intake
    "application_intake": (
        "I can help you start an application. Let's begin with your company "
        "name — you can also reach a relationship manager who will complete the "
        "details with you."
    ),
    # §4.3 Compliance Screener
    "compliance_screener": (
        "Screening is temporarily unavailable. Please retry shortly, or a "
        "compliance officer can run a manual check against OFAC SDN, UN "
        "Consolidated, and EU Sanctions lists."
    ),
    # §4.4 CRM Agent
    "crm_agent": (
        "Your interest has been noted. A relationship manager will follow up "
        "shortly."
    ),
    # §4.5 Appointment Scheduler
    "appointment_scheduler": (
        "I can book you a meeting with a relationship manager. Our next "
        "available slots are on weekday mornings — please retry to confirm one."
    ),
}
