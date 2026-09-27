"""License Advisor agent (design §4.1, R4).

The License Advisor recommends exactly one of four license packages based on
three required attributes — **business activity**, **team size**, and
**timeline**. It is the template for every other specialist agent and doubles
as the system's default/fallback agent (see the Orchestrator and the registry).

Behaviour (design §4.1):

* Elicit the three required attributes. If one or more is missing, ask between
  one and two clarifying questions targeting only the missing attributes
  (R4.1).
* With all three attributes present and within the Growth tier, recommend
  exactly one package from the fixed set with its annual AED cost and a reason,
  in no more than 149 words (R4.2, R4.3, R4.4).
* If the stated requirements exceed the Growth tier (e.g. more than ten visas
  or a larger team than Growth serves), offer a custom quote instead of a fixed
  package (R4.5).

The response is produced by Bedrock **Haiku** for speed (R3.6) through the
shared :func:`bedrock_client.invoke_agent` helper, which times the call and, on
any Bedrock error or timeout, degrades to the predefined fallback response with
``tools_used=["fallback-mode"]`` (R4.6, R10.1, R10.2). On the success path the
agent reports ``tools_used=["bedrock"]`` and attaches
``structured_data={recommended_package, annual_cost_aed, reason}`` when a fixed
package was recommended (R3.5).

On the success path the agent also sets an additive, non-user-visible
``is_recommendation`` flag on the response when the reply is a genuine
recommendation — a fixed package with its AED cost (R4.2, R4.3) or a custom
quote (R4.5). Greetings and clarifying questions leave it absent. The CRM
auto-fire gate consumes this signal so it fires on a real recommendation
instead of on every License Advisor turn (defect C6).
"""

from __future__ import annotations

import re
from typing import Any, Optional

from bedrock_client import HAIKU, invoke_agent
from fallbacks import FALLBACK_RESPONSES

# The registry name for this agent. Kept as a module constant so the
# registration call and the fallback lookup stay in sync.
AGENT_NAME = "license_advisor"

# The Agent_Description read by the Orchestrator for routing (R1.2). Eleven
# words — within the registry's 1-14 word bound (R2.3). Copied verbatim from
# design §4.1.
AGENT_DESCRIPTION = (
    "Recommends one of four license packages by activity, team size, and timeline."
)

# The four fixed packages, verbatim from design §4.1 (R4.2). ``max_visas`` is
# the upper bound the tier serves and is used to detect "exceeds Growth" (R4.5).
PACKAGES: dict[str, dict[str, Any]] = {
    "Idea": {"annual_cost_aed": 6600, "summary": "solo founder", "max_visas": 0},
    "Seed": {"annual_cost_aed": 12500, "summary": "1-2 visas", "max_visas": 2},
    "Startup": {"annual_cost_aed": 22000, "summary": "3 visas + flex office", "max_visas": 3},
    "Growth": {"annual_cost_aed": 45000, "summary": "10 visas + premium office", "max_visas": 10},
}

# The hard cap on a recommendation response (R4.4).
MAX_WORDS = 149

# Bedrock budget for the License Advisor (R4.6 — 10 seconds).
TIMEOUT_S = 10.0


def _system_prompt() -> str:
    """Build the License Advisor system prompt (design §4.1).

    Encodes the three required attributes, the fixed package set with costs,
    the clarifying-question rule (R4.1), the recommendation rule with the
    149-word cap (R4.2-R4.4), and the custom-quote rule for requirements beyond
    Growth (R4.5).
    """
    package_lines = "\n".join(
        f"- {name}: AED {info['annual_cost_aed']:,}/year ({info['summary']})"
        for name, info in PACKAGES.items()
    )
    return (
        "You are the License Advisor for Ask Gulf, a GCC free-zone business "
        "setup authority. Recommend exactly one license package.\n\n"
        "You need THREE attributes from the user before recommending:\n"
        "1. business activity (what the company will do)\n"
        "2. team size (how many people / visas needed)\n"
        "3. timeline (how soon they want to launch)\n\n"
        "The four packages are:\n"
        f"{package_lines}\n\n"
        "Rules:\n"
        "- If one or more of the three attributes is missing, ask ONE or TWO "
        "clarifying questions that target ONLY the missing attributes. Do not "
        "recommend a package yet and do not ask about attributes you already "
        "know.\n"
        "- When you know all three attributes and they fit within the Growth "
        "tier, recommend EXACTLY ONE package. Always state its annual cost in "
        "AED and give a short reason tied to the user's activity, team size, "
        "and timeline.\n"
        "- If the stated requirements exceed the Growth tier (for example more "
        "than ten visas or a team larger than Growth serves), do NOT recommend "
        "a fixed package. Instead offer a custom quote and explain a "
        "relationship manager will prepare it.\n"
        f"- Keep every response to at most {MAX_WORDS} words.\n"
        "- Never mention any internal or confidential project names."
    )


def _cap_words(text: str, limit: int = MAX_WORDS) -> str:
    """Trim ``text`` to at most ``limit`` words (R4.4).

    The shared prompt already instructs the model to stay within the cap; this
    is a defensive guarantee so the contract holds even if the model overshoots.
    Whitespace runs collapse to single spaces so the word count is exact.
    """
    words = text.split()
    if len(words) <= limit:
        return text.strip()
    return " ".join(words[:limit])


def _detect_recommended_package(text: str) -> Optional[str]:
    """Return the package name the response recommends, or ``None``.

    Used to attach ``structured_data`` when the model recommended a fixed
    package (R3.5). A response that only asks clarifying questions (R4.1) or
    offers a custom quote (R4.5) recommends no package, so this returns
    ``None`` and no package structured data is attached.

    Detection is order-sensitive: the longer, more specific names ("Startup")
    are checked before shorter ones so a substring never masks the real match.
    Matching is done on whole words, case-insensitively.
    """
    lowered = text.lower()
    # If the response offers a custom quote, it is not a fixed-package pick.
    if "custom quote" in lowered:
        return None
    # Check most specific names first to avoid partial-word confusion.
    for name in sorted(PACKAGES, key=len, reverse=True):
        if re.search(rf"\b{name.lower()}\b", lowered):
            return name
    return None


def _is_custom_quote(text: str) -> bool:
    """Return ``True`` when the response offers a custom quote (R4.5).

    When the stated requirements exceed the Growth tier the agent does not
    recommend a fixed package; instead it offers a custom quote (design §4.1).
    That reply is still a genuine recommendation for CRM-gating purposes (task
    3.4), even though it attaches no fixed-package ``structured_data``. Matching
    mirrors the marker :func:`_detect_recommended_package` already keys off.
    """
    return "custom quote" in text.lower()


def _is_recommendation(text: str) -> bool:
    """Return ``True`` when a success-path reply is a genuine recommendation.

    A recommendation is a reply that recommends exactly one fixed package with
    its AED cost (R4.2, R4.3 — detected via :func:`_detect_recommended_package`)
    OR offers a custom quote (R4.5). Greetings, clarifying questions, and a
    reply that merely mentions packages without picking one are NOT
    recommendations and this returns ``False``.

    This is the machine-readable signal task 3.5 consumes to gate the CRM
    auto-fire on a real recommendation instead of firing on every License
    Advisor turn. It derives purely from the response text and does not change
    any user-visible reply text.
    """
    return _detect_recommended_package(text) is not None or _is_custom_quote(text)


def _build_structured_data(text: str) -> Optional[dict[str, Any]]:
    """Build ``structured_data`` for a fixed-package recommendation (R3.5).

    Returns ``{recommended_package, annual_cost_aed, reason}`` when the response
    names one of the four packages, otherwise ``None`` (clarifying-question and
    custom-quote responses carry no package structured data).
    """
    package = _detect_recommended_package(text)
    if package is None:
        return None
    return {
        "recommended_package": package,
        "annual_cost_aed": PACKAGES[package]["annual_cost_aed"],
        # The recommendation reason is the model's own response text; the
        # runtime and trace panel surface it as the justification.
        "reason": text,
    }


def handle(user_message: str, history: Optional[list[dict]] = None) -> dict:
    """Handle a License Advisor turn (R4).

    Invokes Bedrock Haiku through the shared helper (which times the call and
    handles the R4.6 fallback), caps the response at 149 words (R4.4), reports
    ``tools_used=["bedrock"]`` on the success path, and attaches
    ``structured_data`` when a fixed package was recommended (R3.5).

    Args:
        user_message: the current user message.
        history: prior conversation turns ({"role","content"}), optional. Used
            so the agent can accumulate the three attributes across turns and
            retain known attributes (R4.6).

    Returns:
        An ``AgentResponse`` dict satisfying the shared contract.
    """
    response = invoke_agent(
        system_prompt=_system_prompt(),
        user_message=user_message,
        history=history,
        model_id=HAIKU,
        tools_used=["bedrock"],
        fallback_text=FALLBACK_RESPONSES[AGENT_NAME],
        timeout_s=TIMEOUT_S,
    )

    # On the fallback path the helper already set tools_used=["fallback-mode"];
    # leave that response untouched so the fallback indication is preserved
    # (R4.6, R10.2). No package structured data is attached to a fallback.
    if response.get("tools_used") == ["fallback-mode"]:
        return response

    # Success path: enforce the 149-word cap (R4.4) and attach structured data
    # for a fixed-package recommendation (R3.5).
    response["text"] = _cap_words(response["text"])
    structured = _build_structured_data(response["text"])
    if structured is not None:
        response["structured_data"] = structured

    # Additive, non-user-visible recommendation signal (task 3.4, defect C6).
    # Set to True when the reply recommends a fixed package with its AED cost
    # (R4.2, R4.3) OR offers a custom quote (R4.5). Task 3.5 gates the CRM
    # auto-fire on this so the CRM fires on a real recommendation instead of on
    # every License Advisor turn; greetings/clarifying questions leave it
    # absent. This is a top-level field so a custom-quote recommendation can
    # carry the signal without attaching fixed-package structured_data
    # (preserving the custom-quote structured_data behaviour, 3.12). This task
    # only ADDS the signal; it does not change CRM gating or reply text.
    if _is_recommendation(response["text"]):
        response["is_recommendation"] = True
    return response


def register_agent() -> None:
    """Register the License Advisor into the Agent_Registry (R2.2).

    Called by the agents package initializer's guarded import loop. Imports
    ``register`` from the ``agents`` package and maps :data:`AGENT_NAME` to this
    module's :data:`AGENT_DESCRIPTION` and :func:`handle`.
    """
    from agents import register

    register(AGENT_NAME, AGENT_DESCRIPTION, handle)
