"""Compliance Screener agent (design §4.3, R6).

Screens a name and an identifier against a mock sanctions / PEP watchlist and
returns a structured, evidence-backed screening result. The design is
deliberately split so the *decision* is deterministic and testable:

* Bedrock (Haiku) is used **only** to parse a name and an identifier out of the
  free-text message (R6.1). If the model cannot produce both, or Bedrock is
  unavailable, a light regex/heuristic parser is used as a safety net so the
  agent never fails the message.
* The **match logic runs in code** — a case-insensitive exact match against the
  watchlist loaded via :func:`mock_data.loader.load_watchlist` (R6.3). A match
  occurs when the name equals a watchlist entry name (ignoring case) OR any
  provided identifier equals any identifier in that entry's ``identifiers``
  list (ignoring case).

If either the name or the identifier is missing, the agent asks for the missing
item and returns without a failure state (R6.2). Otherwise it returns, well
within 5 seconds (R6.4), a ``Screening_Result`` structured payload containing
the result label, name, identifier, status, lists checked, an ISO 8601 UTC
timestamp, and evidence:

* one or more matches  -> status ``flagged`` + manual-review message + evidence
  identifying every matching list and its reason (R6.6, R6.7);
* no match             -> status ``clear`` + cleared-to-proceed message +
  evidence stating no matches were found across the lists checked (R6.8).

Lists checked are always reported as OFAC SDN, UN Consolidated, EU Sanctions
(R6.5). ``tools_used`` is ``["s3"]`` because the watchlist is a data-store read
(local reads a JSON file; deployed reads from S3/mock).
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from agents import register
from bedrock_client import HAIKU, get_bedrock_client, _bedrock_invoke, BedrockError
from fallbacks import FALLBACK_RESPONSES
from mock_data.loader import load_watchlist

# Canonical agent name used as the Agent_Registry key.
AGENT_NAME = "compliance_screener"

# Agent_Description read by the Orchestrator for routing (1-14 words, R2.3).
DESCRIPTION = "Screens a name and identifier against a mock sanctions and PEP watchlist."

# The lists checked are always reported as these three, in this order (R6.5).
LISTS_CHECKED: list[str] = ["OFAC SDN", "UN Consolidated", "EU Sanctions"]

# Screening reads the watchlist data store; reported as S3 (design §4.3).
TOOLS_USED: list[str] = ["s3"]

# System prompt: Bedrock is used ONLY to parse the name/identifier (R6.1).
_PARSE_SYSTEM_PROMPT = (
    "You extract two fields from a compliance screening request: the person's "
    "full name and an identifier (such as a passport, national ID, or reference "
    "number). Respond with ONLY a JSON object of the form "
    '{"name": <string or null>, "identifier": <string or null>}. '
    "Use null for a field that is not present in the message. Do not add any "
    "text outside the JSON object."
)

# Heuristic identifier: a token with letters+digits (e.g. P1234567, RU7654321),
# or a run of 5+ digits. Used as a safety net when Bedrock parsing is
# unavailable so the agent never fails the message.
_IDENTIFIER_RE = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{5,}\b|\b\d{5,}\b")


def _now_iso() -> str:
    """Return the current time as an ISO 8601 UTC timestamp (e.g. 2026-01-16T18:00:00Z)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_with_bedrock(
    user_message: str, bedrock_client: Optional[Any]
) -> tuple[Optional[str], Optional[str]]:
    """Use Bedrock (Haiku) to parse the name and identifier from free text (R6.1).

    Returns ``(name, identifier)`` with either element possibly ``None``. Any
    Bedrock error or malformed output is swallowed and reported as
    ``(None, None)`` so the caller can fall back to heuristics — parsing must
    never fail the message (R6.2).
    """
    try:
        client = bedrock_client if bedrock_client is not None else get_bedrock_client()
        raw = _bedrock_invoke(
            model_id=HAIKU,
            system_prompt=_PARSE_SYSTEM_PROMPT,
            user_message=user_message,
            history=None,
            timeout_s=4.0,
            client=client,
            max_tokens=256,
        )
    except (BedrockError, TimeoutError, Exception):  # noqa: BLE001 - parsing must not fail the message
        return None, None

    return _extract_fields_from_text(raw)


def _extract_fields_from_text(raw: str) -> tuple[Optional[str], Optional[str]]:
    """Pull ``name``/``identifier`` out of the model's JSON reply, tolerating stray text."""
    if not raw:
        return None, None
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None, None
    try:
        obj = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(obj, dict):
        return None, None
    name = _clean(obj.get("name"))
    identifier = _clean(obj.get("identifier"))
    return name, identifier


def _clean(value: Any) -> Optional[str]:
    """Normalise a parsed field to a non-empty stripped string or ``None``."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_with_heuristics(
    user_message: str, name: Optional[str], identifier: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Fill missing name/identifier from the message using simple heuristics.

    Only fills a field the model left empty so a good Bedrock parse is never
    overridden. This keeps the agent responsive and self-sufficient when the
    model is unavailable (R6.2).
    """
    if identifier is None:
        id_match = _IDENTIFIER_RE.search(user_message)
        if id_match:
            identifier = id_match.group(0)

    if name is None:
        name = _guess_name(user_message, identifier)

    return name, identifier


# Common command/filler words that precede a name in a screening request; the
# heuristic name guesser drops these so it does not fold them into the name.
_NAME_STOPWORDS = {
    "screen", "check", "run", "please", "compliance", "on", "for", "the",
    "screening", "verify", "id", "identifier", "passport", "with", "against",
}


def _guess_name(user_message: str, identifier: Optional[str]) -> Optional[str]:
    """Best-effort name guess used only when Bedrock parsing is unavailable.

    Finds a run of capitalised words (title-case like "John Doe" or all-caps
    like "MARIA GARCIA"), after removing the identifier token and stripping any
    leading command/filler words. This is a resilience net; in normal operation
    Bedrock (Haiku) performs the parse (R6.1). The in-code match itself is
    case-insensitive regardless of how the name was parsed (R6.3).
    """
    # Remove the identifier token so it isn't mistaken for a name part.
    text = user_message
    if identifier:
        text = text.replace(identifier, " ")
    # Capitalised word runs: title-case or all-caps, e.g. "John Doe", "MARIA GARCIA".
    candidates = re.findall(
        r"\b(?:[A-Z][a-z]+|[A-Z]{2,})(?:\s+(?:[A-Z][a-z]+|[A-Z]{2,}))+\b", text
    )
    for candidate in candidates:
        words = [w for w in candidate.split() if w.lower() not in _NAME_STOPWORDS]
        if len(words) >= 2:
            return " ".join(words)
    return None


def _find_matches(name: Optional[str], identifier: Optional[str]) -> list[dict]:
    """Return every watchlist entry matched by name or identifier (case-insensitive, R6.3).

    A match occurs when the name equals an entry name (ignoring case) OR the
    provided identifier equals any identifier in the entry's ``identifiers``
    list (ignoring case). The match logic runs entirely in code so it is
    deterministic, fast, and testable.
    """
    name_lower = name.lower() if name else None
    identifier_lower = identifier.lower() if identifier else None

    matches: list[dict] = []
    for entry in load_watchlist():
        entry_name = str(entry.get("name", ""))
        name_hit = name_lower is not None and entry_name.lower() == name_lower

        id_hit = False
        matched_identifier: Optional[str] = None
        if identifier_lower is not None:
            for entry_id in entry.get("identifiers", []) or []:
                if str(entry_id).lower() == identifier_lower:
                    id_hit = True
                    matched_identifier = str(entry_id)
                    break

        if name_hit or id_hit:
            matches.append(
                {
                    "name": entry_name,
                    "list": entry.get("list"),
                    "reason": entry.get("reason"),
                    "matched_on": _matched_on(name_hit, id_hit),
                    "matched_identifier": matched_identifier,
                }
            )
    return matches


def _matched_on(name_hit: bool, id_hit: bool) -> str:
    """Describe which field(s) caused the match, for the evidence trail."""
    if name_hit and id_hit:
        return "name and identifier"
    if name_hit:
        return "name"
    return "identifier"


def _build_screening_result(
    name: str, identifier: str, matches: list[dict]
) -> dict:
    """Assemble the ``Screening_Result`` structured payload (R6.4-R6.8)."""
    timestamp = _now_iso()

    if matches:
        # One or more matches -> flagged + manual review (R6.6, R6.7).
        status = "flagged"
        result_label = "FLAGGED"
        evidence = {
            "summary": "Requires manual review.",
            "matches": [
                {
                    "list": m["list"],
                    "matched_entry_name": m["name"],
                    "matched_on": m["matched_on"],
                    "matched_identifier": m["matched_identifier"],
                    "reason": m["reason"],
                }
                for m in matches
            ],
        }
    else:
        # No match -> clear + cleared to proceed (R6.8).
        status = "clear"
        result_label = "CLEAR"
        evidence = {
            "summary": "No matches were found across the lists checked.",
            "matches": [],
        }

    return {
        "result": result_label,
        "name": name,
        "identifier": identifier,
        "status": status,
        "lists_checked": list(LISTS_CHECKED),
        "timestamp": timestamp,
        "evidence": evidence,
    }


def _render_text(result: dict) -> str:
    """Render a concise user-facing screening message from the structured result."""
    lists = ", ".join(LISTS_CHECKED)
    if result["status"] == "flagged":
        match_count = len(result["evidence"]["matches"])
        lines = [
            f"Screening result for {result['name']} (identifier {result['identifier']}): "
            f"FLAGGED — this entity requires manual review.",
            f"Lists checked: {lists}.",
            f"Found {match_count} matching "
            f"{'entry' if match_count == 1 else 'entries'}:",
        ]
        for m in result["evidence"]["matches"]:
            lines.append(
                f"  • {m['list']} — matched on {m['matched_on']}: {m['reason']}"
            )
        lines.append(f"Screened at {result['timestamp']}.")
        return "\n".join(lines)

    return (
        f"Screening result for {result['name']} (identifier {result['identifier']}): "
        f"CLEAR — this entity is cleared to proceed. "
        f"No matches were found across the lists checked: {lists}. "
        f"Screened at {result['timestamp']}."
    )


def _missing_input_response(
    name: Optional[str], identifier: Optional[str], start: float
) -> dict:
    """Ask for the missing name and/or identifier without a failure state (R6.2).

    When neither field could be parsed at all, the predefined compliance
    fallback response (design §4.3, ``FALLBACK_RESPONSES["compliance_screener"]``)
    is returned; when exactly one is missing, a targeted request naming the
    missing item is returned. In both cases the message is not failed (R6.2).
    """
    if not name and not identifier:
        text = FALLBACK_RESPONSES[AGENT_NAME]
        return {
            "text": text,
            "tools_used": list(TOOLS_USED),
            "time_ms": _elapsed_ms(start),
        }

    missing: list[str] = []
    if not name:
        missing.append("the full name")
    if not identifier:
        missing.append("an identifier (such as a passport or ID number)")
    request = " and ".join(missing)
    text = (
        f"To run a compliance screening I need {request}. "
        "Please provide it and I'll screen against the OFAC SDN, "
        "UN Consolidated, and EU Sanctions lists."
    )
    return {
        "text": text,
        "tools_used": list(TOOLS_USED),
        "time_ms": _elapsed_ms(start),
    }


def _elapsed_ms(start: float) -> int:
    """Whole milliseconds elapsed since ``start`` (never negative)."""
    elapsed = int((time.perf_counter() - start) * 1000)
    return elapsed if elapsed >= 0 else 0


def handle(
    user_message: str,
    conversation_history: Optional[list[dict]] = None,
    *,
    bedrock_client: Optional[Any] = None,
) -> dict:
    """Screen a name/identifier against the watchlist and return an AgentResponse.

    Flow (design §4.3, R6):
        1. Parse a name and identifier from the message — Bedrock (Haiku) for
           parsing only, with a heuristic safety net (R6.1).
        2. If either is missing, ask for it and return without failing (R6.2).
        3. Run the deterministic, case-insensitive exact match in code (R6.3).
        4. Return a structured Screening_Result with result label, name,
           identifier, status, lists checked, timestamp, and evidence within
           5 seconds (R6.4, R6.5); flagged for matches (R6.6, R6.7) or clear
           for none (R6.8).

    Args:
        user_message: the free-text screening request.
        conversation_history: prior turns (unused by the deterministic match;
            accepted for handler-contract uniformity).
        bedrock_client: optional injected Bedrock client (for tests/mocks).

    Returns:
        An ``AgentResponse`` dict with ``tools_used == ["s3"]`` and, on a
        completed screening, ``structured_data`` holding the Screening_Result.
    """
    start = time.perf_counter()

    name, identifier = _parse_with_bedrock(user_message, bedrock_client)
    name, identifier = _parse_with_heuristics(user_message, name, identifier)

    if not name or not identifier:
        return _missing_input_response(name, identifier, start)

    matches = _find_matches(name, identifier)
    result = _build_screening_result(name, identifier, matches)

    return {
        "text": _render_text(result),
        "tools_used": list(TOOLS_USED),
        "time_ms": _elapsed_ms(start),
        "structured_data": result,
    }


def register_agent() -> None:
    """Register the Compliance Screener into the Agent_Registry (R2.2)."""
    register(AGENT_NAME, DESCRIPTION, handle)
