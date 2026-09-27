#!/usr/bin/env python3
"""End-to-end smoke test for the running Ask Gulf stack.

Drives the LIVE backend over HTTP (the same endpoints the container serves) and
checks every agent plus the cross-cutting behaviors:

  - /health and /ping contract
  - License Advisor: recommendation (+ CRM auto-fire), clarifying question
    (no CRM), custom quote for oversized teams
  - Context-aware routing: a multi-turn License Advisor flow where short
    follow-ups ("fintech", "in next month") STAY with license_advisor
  - Application Intake: multi-turn, one field at a time, saved draft
  - Compliance Screener: FLAGGED (watchlist hit) and CLEAR
  - Appointment Scheduler: books the first open slot
  - CRM (direct): captures a lead

Each turn goes through POST /invocations with a stable per-conversation session
id header, so multi-turn flows preserve history exactly like the WebSocket UI.

Usage:  python e2e_test.py [BASE_URL]     (default http://localhost:8080)

Exit code 0 if all checks pass, 1 otherwise. Read-only except that it creates
lead/application records in DynamoDB (same as normal app use).
"""

from __future__ import annotations

import json
import sys
import urllib.request
import uuid

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")

_PASS = 0
_FAIL = 0


def _color(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def check(name: str, ok: bool, detail: str = "") -> bool:
    global _PASS, _FAIL
    if ok:
        _PASS += 1
    else:
        _FAIL += 1
    line = f"[{_color(ok)}] {name}"
    if detail and not ok:
        line += f"\n        -> {detail}"
    print(line)
    return ok


def get(path: str) -> tuple[int, dict]:
    req = urllib.request.Request(f"{BASE}{path}", method="GET")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def invoke(text: str, session_id: str) -> dict:
    """POST one turn to /invocations with a stable session id header."""
    body = json.dumps({"text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/invocations",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": session_id,
        },
    )
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read().decode("utf-8"))


def agents_in(result: dict) -> list[str]:
    return [e.get("agent_selected") for e in result.get("trace", [])]


def tools_for(result: dict, agent: str) -> list[str]:
    for e in result.get("trace", []):
        if e.get("agent_selected") == agent:
            return e.get("tools_used", [])
    return []


def section(title: str) -> None:
    print(f"\n=== {title} ===")


# --------------------------------------------------------------------------- #
# 1. Health / ping contract
# --------------------------------------------------------------------------- #
section("Health & contract")
try:
    status, body = get("/health")
    check("/health returns 200", status == 200, f"status={status}")
    check("/health reports 5 agents", body.get("registered_agents") == 5, str(body))
    status, body = get("/ping")
    check("/ping returns Healthy", body.get("status") == "Healthy", str(body))
except Exception as exc:  # noqa: BLE001
    check("backend reachable", False, str(exc))
    print("\nBackend not reachable — is the stack up? `docker compose up -d`")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# 2. License Advisor — recommendation (+ CRM auto-fire)
# --------------------------------------------------------------------------- #
section("License Advisor — recommendation + CRM auto-fire")
sid = f"e2e-rec-{uuid.uuid4()}"
r = invoke("tech consulting company, 3 people, launching next month", sid)
ag = agents_in(r)
check("routed to license_advisor", "license_advisor" in ag, str(ag))
check("CRM auto-fired on recommendation", "crm_agent" in ag, str(ag))
check("CRM wrote a lead (dynamodb, not fallback)",
      tools_for(r, "crm_agent") == ["dynamodb"], str(tools_for(r, "crm_agent")))
check("reply has no 'Lead ... captured' pollution",
      "captured" not in r.get("message", "").lower(), r.get("message", "")[:120])
check("timings reported", isinstance(r.get("routing_ms"), int) and isinstance(r.get("total_ms"), int),
      f"routing_ms={r.get('routing_ms')} total_ms={r.get('total_ms')}")


# --------------------------------------------------------------------------- #
# 3. License Advisor — clarifying question (NO CRM)
# --------------------------------------------------------------------------- #
section("License Advisor — clarifying question (no CRM)")
sid = f"e2e-clarify-{uuid.uuid4()}"
r = invoke("I need help choosing a license", sid)
ag = agents_in(r)
check("routed to license_advisor", "license_advisor" in ag, str(ag))
check("CRM did NOT fire (not a recommendation)", "crm_agent" not in ag, str(ag))


# --------------------------------------------------------------------------- #
# 4. License Advisor — custom quote (exceeds Growth)
# --------------------------------------------------------------------------- #
section("License Advisor — custom quote (25 visas)")
sid = f"e2e-custom-{uuid.uuid4()}"
r = invoke("I need 25 visas for a large trading operation", sid)
ag = agents_in(r)
msg = r.get("message", "").lower()
check("routed to license_advisor", "license_advisor" in ag, str(ag))
check("offers a custom quote", "custom quote" in msg or "relationship manager" in msg,
      r.get("message", "")[:160])


# --------------------------------------------------------------------------- #
# 5. Context-aware routing — multi-turn LA flow stays with LA
# --------------------------------------------------------------------------- #
section("Context-aware routing — multi-turn stays with license_advisor")
sid = f"e2e-ctx-{uuid.uuid4()}"
r1 = invoke("hi i want to start the tech company", sid)
check("turn 1 -> license_advisor", "license_advisor" in agents_in(r1), str(agents_in(r1)))
r2 = invoke("fintech", sid)
check("turn 2 'fintech' -> license_advisor (not misrouted)",
      "license_advisor" in agents_in(r2) and "compliance_screener" not in agents_in(r2),
      str(agents_in(r2)))
r3 = invoke("in next month", sid)
check("turn 3 'in next month' -> license_advisor (not appointment_scheduler)",
      "license_advisor" in agents_in(r3) and "appointment_scheduler" not in agents_in(r3),
      str(agents_in(r3)))


# --------------------------------------------------------------------------- #
# 6. Application Intake — multi-turn, one field at a time
# --------------------------------------------------------------------------- #
section("Application Intake — multi-turn draft")
sid = f"e2e-intake-{uuid.uuid4()}"
r = invoke("I want to start my business setup application", sid)
check("intake starts, asks company name",
      "company name" in r.get("message", "").lower(), r.get("message", "")[:120])
for answer in ["Gulf Tech LLC", "software development", "Startup", "3", "Indian", "founder@gulftech.example"]:
    r = invoke(answer, sid)
    check(f"intake stays with application_intake after '{answer[:20]}'",
          "application_intake" in agents_in(r), str(agents_in(r)))
msg = r.get("message", "").lower()
check("intake completes with a saved draft",
      "draft" in msg or "relationship manager" in msg or "app-" in msg, r.get("message", "")[:200])


# --------------------------------------------------------------------------- #
# 7. Compliance Screener — FLAGGED and CLEAR
# --------------------------------------------------------------------------- #
section("Compliance Screener")
sid = f"e2e-comp-flag-{uuid.uuid4()}"
r = invoke("Run a compliance screening on John Doe, passport P1234567", sid)
check("flagged case -> compliance_screener", "compliance_screener" in agents_in(r), str(agents_in(r)))
check("John Doe FLAGGED", "flagged" in r.get("message", "").lower(), r.get("message", "")[:160])

sid = f"e2e-comp-clear-{uuid.uuid4()}"
r = invoke("Screen Jane Smith, ID Z1112223", sid)
check("clear case -> compliance_screener", "compliance_screener" in agents_in(r), str(agents_in(r)))
check("Jane Smith CLEAR", "clear" in r.get("message", "").lower(), r.get("message", "")[:160])


# --------------------------------------------------------------------------- #
# 8. Appointment Scheduler
# --------------------------------------------------------------------------- #
section("Appointment Scheduler")
sid = f"e2e-appt-{uuid.uuid4()}"
r = invoke("Book me a meeting with a relationship manager", sid)
check("routed to appointment_scheduler", "appointment_scheduler" in agents_in(r), str(agents_in(r)))
check("booking confirmed with a slot reference",
      "slot-" in r.get("message", "").lower() or "booked" in r.get("message", "").lower(),
      r.get("message", "")[:160])


# --------------------------------------------------------------------------- #
# 9. CRM (direct)
# --------------------------------------------------------------------------- #
section("CRM (direct lead capture)")
sid = f"e2e-crm-{uuid.uuid4()}"
r = invoke("Please capture my details as a lead", sid)
check("routed to crm_agent", "crm_agent" in agents_in(r), str(agents_in(r)))
check("CRM wrote a real lead (dynamodb)", "dynamodb" in tools_for(r, "crm_agent"),
      str(tools_for(r, "crm_agent")))


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
print(f"\n{'='*48}\n  RESULT: {_PASS} passed, {_FAIL} failed\n{'='*48}")
sys.exit(0 if _FAIL == 0 else 1)
