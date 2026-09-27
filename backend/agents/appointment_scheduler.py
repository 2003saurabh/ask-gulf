"""Appointment Scheduler agent for Ask Gulf (design §4.5, R8).

Books a meeting slot with a Relationship_Manager. The agent is deterministic:
it reads available slots from the Calendar mock data (each slot has
``slot_id``, ``date``, ``time``, ``rm_name``, ``booked``; R8.1), selects the
first slot whose ``booked`` flag is not set (R8.2), and returns the confirmed
slot details plus a calendar event as structured data (R8.3).

The calendar read is reported as an S3 tool use (``tools_used=["s3"]``) per
design §4.5: locally the loader reads a JSON file, and the deployed
environment reads the same data seeded into S3.

If no unbooked slot is available (or the calendar cannot be read), the agent
degrades to the predefined Fallback_Response for ``appointment_scheduler``
(R10.1–10.3) with ``tools_used=["fallback-mode"]``, so the demo never stalls.
"""

from __future__ import annotations

import time
from typing import Optional

from bedrock_client import AgentResponse
from fallbacks import FALLBACK_RESPONSES
from mock_data.loader import load_calendar

import agents

# The Agent_Description read by the Orchestrator for routing (R2.1, 1–14 words).
_DESCRIPTION = "Books an available slot with a relationship manager from the calendar."

_AGENT_NAME = "appointment_scheduler"


def register_agent() -> None:
    """Register the Appointment Scheduler into the Agent_Registry (R2.2)."""
    agents.register(_AGENT_NAME, _DESCRIPTION, handle)


def _elapsed_ms(start: float) -> int:
    """Return whole milliseconds elapsed since ``start`` (never negative)."""
    elapsed = int((time.perf_counter() - start) * 1000)
    return elapsed if elapsed >= 0 else 0


def _select_open_slot(slots: list[dict]) -> Optional[dict]:
    """Return the first slot whose ``booked`` flag is not set (R8.2).

    A slot is considered open when its ``booked`` value is falsy (typically
    ``false``). Slots missing the flag are treated as open.
    """
    for slot in slots:
        if not slot.get("booked", False):
            return slot
    return None


def _build_calendar_event(slot: dict) -> dict:
    """Build a calendar event for a confirmed slot (R8.3)."""
    return {
        "summary": f"Ask Gulf meeting with {slot.get('rm_name')}",
        "date": slot.get("date"),
        "time": slot.get("time"),
        "rm_name": slot.get("rm_name"),
        "slot_id": slot.get("slot_id"),
        "status": "confirmed",
    }


def _fallback(start: float) -> AgentResponse:
    """Return the predefined fallback response (R10.1–10.3)."""
    return AgentResponse(
        text=FALLBACK_RESPONSES[_AGENT_NAME],
        tools_used=["fallback-mode"],
        time_ms=_elapsed_ms(start),
    )


def handle(user_message: str, conversation_history: Optional[list[dict]] = None) -> AgentResponse:
    """Book the first available slot and confirm it (R8).

    Reads slots from the Calendar (R8.1), selects one whose ``booked`` flag is
    not set (R8.2), and returns the confirmed slot details and a calendar event
    as structured data (R8.3). ``tools_used`` is ``["s3"]`` for the calendar
    read. On any failure — the calendar cannot be read or no open slot exists —
    the agent returns the predefined fallback response.

    Args:
        user_message: the current user message (unused by the deterministic
            booking logic; accepted to satisfy the uniform handler contract).
        conversation_history: prior turns (unused; accepted for contract
            uniformity).

    Returns:
        An ``AgentResponse`` satisfying the contract, with ``structured_data``
        ``{slot_id, date, time, rm_name, calendar_event}`` on success.
    """
    start = time.perf_counter()

    try:
        slots = load_calendar()  # R8.1 — read available slots (reported as S3)
    except Exception:  # noqa: BLE001 - degrade to fallback so the demo never stalls
        return _fallback(start)

    slot = _select_open_slot(slots)  # R8.2 — pick a slot whose booked flag is unset
    if slot is None:
        return _fallback(start)

    calendar_event = _build_calendar_event(slot)  # R8.3

    text = (
        f"You're booked with {slot.get('rm_name')} on {slot.get('date')} "
        f"at {slot.get('time')}. Your confirmation reference is "
        f"{slot.get('slot_id')}."
    )

    return AgentResponse(
        text=text,
        tools_used=["s3"],
        time_ms=_elapsed_ms(start),
        structured_data={
            "slot_id": slot.get("slot_id"),
            "date": slot.get("date"),
            "time": slot.get("time"),
            "rm_name": slot.get("rm_name"),
            "calendar_event": calendar_event,
        },
    )
