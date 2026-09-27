"""Application Intake agent (design §4.2, R5, R14.5).

The Application Intake agent guides an applicant through a business-setup
application **one field at a time**, in a fixed order, and — once all six
fields are collected — presents a labelled summary and saves a ``DRAFT``
application record to the DynamoDB ``ask-gulf-leads`` table.

Because intake is *stateful across turns* but the runtime hands each invocation
the full conversation ``history``, this module derives the collected state
purely from that history rather than from any server-side session dict. On each
turn it:

1. Reconstructs which fields have already been collected by replaying prior
   turns (the assistant asks for one field, the user answers it) — see
   :func:`_derive_state`.
2. Treats the current ``user_message`` as the answer to the currently requested
   field, validating and storing it (R5.2), or rejecting it while retaining the
   prior values (R5.3, R5.5, R5.6).
3. Advances to the next uncollected field, or — when all six are present —
   summarises and persists the draft (R5.7–R5.10).

Field order (R5.1):
    company name, business activity, package type, number of shareholders,
    primary shareholder nationality, contact email.

Validations:
    * number of shareholders: integer in ``[1, 50]`` inclusive (R5.5).
    * contact email: a single ``@`` separating a non-empty local part from a
      domain containing at least one ``.``, and at most 254 characters (R5.6).

On the completion turn ``tools_used == ["bedrock", "dynamodb"]`` and the
response carries ``structured_data`` with the draft record shape
``{application_id, status:"DRAFT", fields:{...}, created_at}`` (design §4.2).
On a DynamoDB save failure the collected values are retained, the user is told
the draft could not be saved, and success is never claimed (R5.9).

No AWS credentials appear here; boto3 is imported lazily and resolves
credentials from the SSO profile (local) or the AgentCore execution role
(deployed) per :mod:`config` (R16).
"""

from __future__ import annotations

import random
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from bedrock_client import AgentResponse, _elapsed_ms

import agents
from config import config
from fallbacks import FALLBACK_RESPONSES

# --------------------------------------------------------------------------- #
# Agent identity / registry
# --------------------------------------------------------------------------- #

AGENT_NAME = "application_intake"

# Description read by the Orchestrator for routing (1-14 words, R2.3).
DESCRIPTION = "Collects application fields one at a time and saves a draft application."


def register_agent() -> None:
    """Register this agent into the ``AGENT_REGISTRY`` (R2.2).

    Called by the agents package initializer via ``register_agent()``; delegates
    to :func:`agents.register`, which validates the description and rejects
    duplicates.
    """
    agents.register(AGENT_NAME, DESCRIPTION, handle)


# --------------------------------------------------------------------------- #
# Field definitions and order (R5.1)
# --------------------------------------------------------------------------- #

# Each field: (key, human label). The order is the collection order (R5.1).
FIELDS: tuple[tuple[str, str], ...] = (
    ("company_name", "Company name"),
    ("business_activity", "Business activity"),
    ("package_type", "Package type"),
    ("number_of_shareholders", "Number of shareholders"),
    ("primary_shareholder_nationality", "Primary shareholder nationality"),
    ("contact_email", "Contact email"),
)

FIELD_KEYS: tuple[str, ...] = tuple(key for key, _ in FIELDS)
FIELD_LABELS: dict[str, str] = {key: label for key, label in FIELDS}

# The prompt text used to request each field. The exact "please provide ..."
# wording also serves as the marker :func:`_derive_state` scans for in prior
# assistant turns to reconstruct which field was last requested.
FIELD_PROMPTS: dict[str, str] = {
    "company_name": "Let's start your application. What is your company name?",
    "business_activity": "What is your business activity?",
    "package_type": "Which package type would you like? (Idea, Seed, Startup, or Growth)",
    "number_of_shareholders": "How many shareholders will the company have? (an integer between 1 and 50)",
    "primary_shareholder_nationality": "What is the primary shareholder's nationality?",
    "contact_email": "What is the best contact email for the application?",
}

# Expected-format hints shown when a field fails validation (R5.3).
FIELD_FORMATS: dict[str, str] = {
    "number_of_shareholders": "a whole number between 1 and 50 inclusive",
    "contact_email": (
        "a valid email address of the form local@domain.tld (a single '@', a "
        "non-empty local part, a domain containing at least one '.', and at "
        "most 254 characters)"
    ),
}

# Email length bound (R5.6).
_EMAIL_MAX_LEN = 254

# Shareholder bounds (R5.5).
_SHAREHOLDERS_MIN = 1
_SHAREHOLDERS_MAX = 50


# --------------------------------------------------------------------------- #
# Validators (R5.5, R5.6)
# --------------------------------------------------------------------------- #


def validate_shareholders(value: str) -> tuple[bool, Optional[int]]:
    """Validate ``number of shareholders`` as an integer in [1, 50] (R5.5).

    Accepts a string that represents a base-10 integer between 1 and 50
    inclusive. Rejects non-integers, out-of-range values, floats such as
    ``"2.0"``, and signed/whitespace-padded values only when they still parse
    to a clean integer.

    Returns:
        ``(True, parsed_int)`` when valid, else ``(False, None)``.
    """
    if not isinstance(value, str):
        return False, None
    candidate = value.strip()
    # Only a plain optional-sign integer is accepted (no ".", "e", etc.).
    if not re.fullmatch(r"[+-]?\d+", candidate):
        return False, None
    parsed = int(candidate)
    if _SHAREHOLDERS_MIN <= parsed <= _SHAREHOLDERS_MAX:
        return True, parsed
    return False, None


def validate_email(value: str) -> bool:
    """Validate ``contact email`` per R5.6.

    Accepted iff the value has exactly one ``@`` separating a non-empty local
    part from a domain part that contains at least one ``.``, and the whole
    string is at most 254 characters. Whitespace is not permitted inside the
    address.
    """
    if not isinstance(value, str):
        return False
    candidate = value.strip()
    if not candidate or len(candidate) > _EMAIL_MAX_LEN:
        return False
    if any(ch.isspace() for ch in candidate):
        return False
    if candidate.count("@") != 1:
        return False
    local, _, domain = candidate.partition("@")
    if not local:
        return False
    if "." not in domain:
        return False
    # A domain cannot start or end with '.' and must have a non-empty label
    # on both sides of at least one dot.
    if domain.startswith(".") or domain.endswith("."):
        return False
    return True


def _validate_field(key: str, raw_value: str) -> tuple[bool, Any, Optional[str]]:
    """Validate ``raw_value`` for ``key``.

    Returns ``(ok, stored_value, error_hint)``. ``stored_value`` is the value to
    persist when ``ok`` (an int for shareholders, otherwise the trimmed string).
    ``error_hint`` is the expected-format string when invalid (R5.3).
    """
    value = raw_value.strip()
    if key == "number_of_shareholders":
        ok, parsed = validate_shareholders(value)
        if ok:
            return True, parsed, None
        return False, None, FIELD_FORMATS["number_of_shareholders"]
    if key == "contact_email":
        if validate_email(value):
            return True, value, None
        return False, None, FIELD_FORMATS["contact_email"]
    # Free-text fields (company name, business activity, package type,
    # nationality): accepted iff non-empty after trimming.
    if value:
        return True, value, None
    return False, None, "a non-empty value"


# --------------------------------------------------------------------------- #
# State derivation from conversation history (design §4.2)
# --------------------------------------------------------------------------- #


def _next_uncollected(collected: dict[str, Any]) -> Optional[str]:
    """Return the first field key (in order) not yet in ``collected``."""
    for key in FIELD_KEYS:
        if key not in collected:
            return key
    return None


def _requested_field_from_text(text: str) -> Optional[str]:
    """Identify which field an assistant turn was requesting, if any.

    Matches the prompt text emitted by :data:`FIELD_PROMPTS`. Returns the field
    key or ``None`` when the assistant turn was not a field request (e.g. the
    final summary).
    """
    if not isinstance(text, str):
        return None
    for key, prompt in FIELD_PROMPTS.items():
        if prompt in text:
            return key
    return None


def _derive_state(history: Optional[list[dict]]) -> dict[str, Any]:
    """Reconstruct collected field values from prior conversation turns.

    Replays the history: whenever an assistant turn requested field ``F`` and
    the immediately following user turn provides a value that passes validation
    for ``F``, that value is recorded as collected. Invalid answers are ignored
    for state purposes (the field remains uncollected), which is what preserves
    "retain prior values and re-request" behaviour across turns (R5.3).

    Only the value for the field that was actually requested is taken from each
    user turn, so extra values in a multi-field answer are ignored (R5.4).

    Returns:
        An ordered-by-insertion dict of ``{field_key: stored_value}`` for every
        field collected so far.
    """
    collected: dict[str, Any] = {}
    turns = history or []
    pending_field: Optional[str] = None
    for turn in turns:
        role = turn.get("role")
        content = turn.get("content", "")
        if role == "assistant":
            pending_field = _requested_field_from_text(content)
        elif role == "user":
            if pending_field is not None and pending_field not in collected:
                ok, stored, _hint = _validate_field(pending_field, content or "")
                if ok:
                    collected[pending_field] = stored
            pending_field = None
    return collected


# --------------------------------------------------------------------------- #
# DynamoDB persistence (R5.8, R14.5)
# --------------------------------------------------------------------------- #


def _generate_application_id() -> str:
    """Generate an application id of the form ``APP-2026-XXXX`` (R5.8).

    ``XXXX`` is a zero-padded four-digit sequence 0000-9999. A random sequence
    is sufficient for the demo (no cross-request coordination required).
    """
    return f"APP-2026-{random.randint(0, 9999):04d}"


def _now_iso_utc() -> str:
    """Return the current time as an ISO 8601 UTC timestamp (R5.8)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _default_dynamodb_table():
    """Return the reused DynamoDB ``Table`` resource for ``config.dynamodb_table`` (C3, R1.6).

    Delegates to the shared per-thread cache in :mod:`agents._dynamo` so the
    boto3 resource/Table is built once per thread and reused across draft saves
    instead of being rebuilt on every call. boto3 is imported lazily there, so
    this module stays import-safe without AWS credentials; the credential source
    (SSO profile locally, AgentCore execution role deployed) is unchanged
    (R16, 3.13). An injected ``dynamodb_table`` bypasses this entirely.
    """
    from agents._dynamo import get_table

    return get_table(config.dynamodb_table)


def _save_draft(
    record: dict[str, Any],
    *,
    dynamodb_table: Optional[Any] = None,
) -> bool:
    """Persist a draft application item to DynamoDB (R5.8).

    Builds the single-table item shape (``pk == application_id``,
    ``entity_type == "application"``, all six field values, ``status``,
    ``created_at``) and calls ``put_item``. Returns ``True`` on success and
    ``False`` on any failure so the caller can honour R5.9 (never claim success
    on failure).

    Args:
        record: the ``structured_data`` draft record
            ``{application_id, status, fields, created_at}``.
        dynamodb_table: optional injected DynamoDB Table (used by tests/mocks).
    """
    fields = record["fields"]
    item = {
        "pk": record["application_id"],
        "entity_type": "application",
        "application_id": record["application_id"],
        "status": record["status"],
        "created_at": record["created_at"],
        **fields,
    }
    try:
        table = dynamodb_table if dynamodb_table is not None else _default_dynamodb_table()
        table.put_item(Item=item)
        return True
    except Exception:  # noqa: BLE001 - any boto/network error is a save failure (R5.9)
        return False


# --------------------------------------------------------------------------- #
# Response builders
# --------------------------------------------------------------------------- #


def _build_summary(fields: dict[str, Any]) -> str:
    """Build the labelled summary of all six collected fields (R5.7)."""
    lines = ["Here is a summary of your application:"]
    for key in FIELD_KEYS:
        lines.append(f"- {FIELD_LABELS[key]}: {fields[key]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Handler (R5)
# --------------------------------------------------------------------------- #


def handle(
    user_message: str,
    conversation_history: Optional[list[dict]] = None,
    *,
    dynamodb_table: Optional[Any] = None,
) -> AgentResponse:
    """Collect one application field per turn and save a draft when complete (R5).

    The collected state is derived from ``conversation_history`` (design §4.2):
    prior turns tell us which fields are already collected and which field the
    current ``user_message`` answers. The message is validated and stored
    (R5.2) or rejected while retaining prior values (R5.3, R5.5, R5.6); only the
    currently requested field is taken from a multi-value answer (R5.4). When
    all six are collected the agent summarises (R5.7) and saves a ``DRAFT`` to
    DynamoDB (R5.8), reporting failure without claiming success (R5.9) or, on
    success, that a relationship manager will make contact within 24 hours
    (R5.10).

    Args:
        user_message: the current user message (the answer to the pending field).
        conversation_history: prior turns ({"role","content"}); the source of
            truth for collected state.
        dynamodb_table: optional injected DynamoDB Table (used by tests/mocks).

    Returns:
        An ``AgentResponse``. On completion, ``tools_used == ["bedrock",
        "dynamodb"]`` and ``structured_data`` carries the draft record.
    """
    start = time.perf_counter()

    try:
        collected = _derive_state(conversation_history)

        # Determine which field this message answers: the first uncollected
        # field at the *start* of this turn (i.e. the one last requested).
        pending = _next_uncollected(collected)

        if pending is None:
            # All six were already collected on a previous turn (e.g. the draft
            # was saved already). Re-present the summary without re-saving.
            summary = _build_summary(collected)
            text = (
                f"{summary}\n\nYour application draft is complete. A "
                "relationship manager will make contact within 24 hours."
            )
            return AgentResponse(
                text=text,
                tools_used=["bedrock"],
                time_ms=_elapsed_ms(start),
            )

        # Is this the very first intake turn (nothing requested yet and no prior
        # intake prompt in history)? Then request the first field rather than
        # treating the message as an answer.
        if not _history_has_intake_prompt(conversation_history):
            return AgentResponse(
                text=FIELD_PROMPTS[pending],
                tools_used=["bedrock"],
                time_ms=_elapsed_ms(start),
            )

        # Validate the current message against the pending field.
        ok, stored, hint = _validate_field(pending, user_message or "")
        if not ok:
            # R5.3/R5.5/R5.6: reject, retain prior values, state which field is
            # invalid and its expected format, and re-request the same field.
            text = (
                f"That value for '{FIELD_LABELS[pending]}' isn't valid. "
                f"Expected {hint}. {FIELD_PROMPTS[pending]}"
            )
            return AgentResponse(
                text=text,
                tools_used=["bedrock"],
                time_ms=_elapsed_ms(start),
            )

        # Valid: store it and advance (R5.2). Only this field's value is taken
        # from the message, so extra values are ignored (R5.4).
        collected[pending] = stored
        next_field = _next_uncollected(collected)

        if next_field is not None:
            # More fields to collect: request the next one (R5.1, R5.2).
            return AgentResponse(
                text=FIELD_PROMPTS[next_field],
                tools_used=["bedrock"],
                time_ms=_elapsed_ms(start),
            )

        # All six collected: summarise (R5.7) and save the draft (R5.8).
        summary = _build_summary(collected)
        record = {
            "application_id": _generate_application_id(),
            "status": "DRAFT",
            "fields": dict(collected),
            "created_at": _now_iso_utc(),
        }
        saved = _save_draft(record, dynamodb_table=dynamodb_table)

        if not saved:
            # R5.9: retain values, say the draft could not be saved, do not
            # claim success. tools_used still reflects the attempted stores.
            text = (
                f"{summary}\n\nI'm sorry — I couldn't save your application "
                "draft just now. Your details have been kept; please try again "
                "shortly."
            )
            return AgentResponse(
                text=text,
                tools_used=["bedrock", "dynamodb"],
                time_ms=_elapsed_ms(start),
            )

        # R5.10: success — inform the user an RM will contact within 24 hours.
        text = (
            f"{summary}\n\nYour draft application {record['application_id']} has "
            "been saved. A relationship manager will make contact within 24 "
            "hours."
        )
        return AgentResponse(
            text=text,
            tools_used=["bedrock", "dynamodb"],
            time_ms=_elapsed_ms(start),
            structured_data=record,
        )
    except Exception:  # noqa: BLE001 - never fail the message; degrade to fallback
        return AgentResponse(
            text=FALLBACK_RESPONSES[AGENT_NAME],
            tools_used=["fallback-mode"],
            time_ms=_elapsed_ms(start),
        )


def _history_has_intake_prompt(history: Optional[list[dict]]) -> bool:
    """Return True if any prior assistant turn already requested an intake field.

    Used to distinguish the first intake turn (where the incoming message is the
    user's routed request, not a field answer) from later turns (where the
    message answers the previously requested field).
    """
    for turn in history or []:
        if turn.get("role") == "assistant" and _requested_field_from_text(
            turn.get("content", "")
        ):
            return True
    return False
