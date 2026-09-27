"""CRM Agent for Ask Gulf (design §4.4, R7).

The CRM_Agent captures a lead after a licensing conversation. The runtime
auto-fires it in the background once the License Advisor completes, for the same
user message, producing a second Trace_Entry (R7.1, R7.5); it is also routable
directly through the Agent_Registry.

On invocation the agent creates a lead record in DynamoDB
(``ask-gulf-leads``) with (R7.2):

* ``lead_id`` = ``LEAD-<timestamp>``,
* ``source`` = ``aws-summit-demo``,
* ``status`` = ``NEW``,
* an ``interaction_summary`` derived from the user message,
* a ``created_at`` creation timestamp in ISO 8601 UTC.

The record is persisted through the DynamoDB tool (R7.3), and the agent returns
a confirmation with the lead record as ``structured_data`` (R7.4). The single
-table shape mirrors the design's Data Models section: ``pk`` = ``lead_id`` and
``entity_type`` = ``"lead"``.

Resilience (R10.3): if the DynamoDB write fails, the agent degrades to the
predefined ``FALLBACK_RESPONSES["crm_agent"]`` string with
``tools_used == ["fallback-mode"]`` rather than failing the message. boto3 is
imported lazily and the table name comes from :mod:`config`, so importing this
module never requires AWS credentials (R16).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from bedrock_client import AgentResponse
from config import config
from fallbacks import FALLBACK_RESPONSES

# Fixed lead attributes from the design (§4.4, R7.2).
_LEAD_SOURCE = "aws-summit-demo"
_LEAD_STATUS = "NEW"

# Registry description (R2.1, R2.3): must be 1-14 words. Copied from design §4.4.
_DESCRIPTION = "Creates a NEW lead record in DynamoDB after a licensing conversation."

# Cap the stored interaction summary so a long message stays a summary.
_SUMMARY_MAX_CHARS = 280


def register_agent() -> None:
    """Register the CRM agent into the Agent_Registry (R2.2, R2.4)."""
    from agents import register

    register(name="crm_agent", description=_DESCRIPTION, handler=handle)


def _elapsed_ms(start: float) -> int:
    """Return whole milliseconds elapsed since ``start`` (never negative)."""
    elapsed = int((time.perf_counter() - start) * 1000)
    return elapsed if elapsed >= 0 else 0


def _now_utc_iso() -> str:
    """Return the current time as an ISO 8601 UTC string (e.g. ...Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _lead_timestamp() -> int:
    """Return the integer epoch seconds used in the ``LEAD-<timestamp>`` id."""
    return int(time.time())


def _summarize_interaction(user_message: str) -> str:
    """Derive a concise interaction summary from the user message (R7.2)."""
    text = (user_message or "").strip()
    if not text:
        return "Licensing enquiry captured from the Ask Gulf demo."
    if len(text) > _SUMMARY_MAX_CHARS:
        return text[: _SUMMARY_MAX_CHARS - 1].rstrip() + "\u2026"
    return text


def _default_table():
    """Return the reused DynamoDB Table resource (C3, R1.6).

    Delegates to the shared per-thread cache in :mod:`agents._dynamo` so the
    boto3 resource/Table is built once per thread and reused across lead writes
    instead of being rebuilt on every call. boto3 is imported lazily there, so
    importing this module never forces a boto3 dependency or live AWS
    credentials. The credential source (SSO profile locally, default chain
    deployed) is unchanged (3.13); an injected ``table`` bypasses this entirely.
    """
    from agents._dynamo import get_table

    return get_table(config.dynamodb_table)


def handle(
    user_message: str,
    conversation_history: Optional[list[dict]] = None,
    *,
    table: Optional[Any] = None,
) -> AgentResponse:
    """Create a NEW lead record in DynamoDB and confirm it (R7.2, R7.3, R7.4).

    Args:
        user_message: the user message that prompted the lead capture; used to
            derive the interaction summary.
        conversation_history: prior turns (unused for the write; accepted to
            satisfy the uniform handler contract, R3.2).
        table: an optional injected DynamoDB Table resource (for tests/mocks).

    Returns:
        An ``AgentResponse`` with a confirmation ``text``, ``tools_used``
        of ``["dynamodb"]`` on success, and the lead record as
        ``structured_data``. On a DynamoDB failure it degrades to the
        predefined fallback with ``tools_used == ["fallback-mode"]`` (R10.3).
    """
    start = time.perf_counter()

    lead_id = f"LEAD-{_lead_timestamp()}"
    lead = {
        "lead_id": lead_id,
        "source": _LEAD_SOURCE,
        "status": _LEAD_STATUS,
        "interaction_summary": _summarize_interaction(user_message),
        "created_at": _now_utc_iso(),
    }

    # Single-table item shape (design Data Models): pk = lead_id, entity_type.
    item = {"pk": lead_id, "entity_type": "lead", **lead}

    try:
        dest = table if table is not None else _default_table()
        dest.put_item(Item=item)
    except Exception:  # noqa: BLE001 - any write failure degrades gracefully (R10.3)
        return AgentResponse(
            text=FALLBACK_RESPONSES["crm_agent"],
            tools_used=["fallback-mode"],
            time_ms=_elapsed_ms(start),
        )

    confirmation = (
        f"Lead {lead_id} captured. A relationship manager will follow up shortly."
    )
    return AgentResponse(
        text=confirmation,
        tools_used=["dynamodb"],
        time_ms=_elapsed_ms(start),
        structured_data=lead,
    )
