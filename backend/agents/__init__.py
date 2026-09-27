"""Import-safe Agent Registry for Ask Gulf (design §2, R2).

This package initializer builds ``AGENT_REGISTRY``: a dict mapping each unique
agent ``name`` to a record ``{"description": str, "handler": callable}`` (R2.1).
Registration is designed to be resilient so the system never fails to start
because one agent is missing or misbehaves:

* On package load, each in-scope agent module is imported inside a guarded loop
  and asked to register itself (R2.2). A module that raises during import is
  skipped, every successfully imported agent is retained, and an error
  indication naming the failed agent is recorded (R2.6).
* Each ``Agent_Description`` is validated to a maximum of fourteen words and a
  minimum of one word; a violation excludes the agent and records an error
  (R2.3).
* A duplicate registration for an existing name is rejected, the first record
  is kept, and an error indication naming the duplicate is recorded (R2.7).

Zero-touch extensibility (R2.4): a new agent is added by creating
``agents/<name>.py`` that exposes ``register_agent()`` (which calls
``register(...)``) and appending ``<name>`` to ``_IN_SCOPE``. The Orchestrator
reads this registry at decision time (R2.5) and needs no changes.

The teammate agents (Document Verifier, Fee Calculator, Knowledge Agent, Status
Tracker, Renewal Agent, Feedback Agent) are out of scope here; they append
their module names to ``_IN_SCOPE`` when added.
"""

from __future__ import annotations

import importlib
from typing import Callable

# name -> {"description": str, "handler": callable}  (R2.1)
AGENT_REGISTRY: dict[str, dict] = {}

# Human-readable error indications accumulated during registration/import.
# Each entry names the failed or duplicate agent (R2.6, R2.7) or the invalid
# description (R2.3).
_REGISTRATION_ERRORS: list[str] = []

# Description word bounds (R2.3): minimum one word, maximum fourteen words.
_MIN_WORDS = 1
_MAX_WORDS = 14


def register(name: str, description: str, handler: Callable) -> None:
    """Register one agent into ``AGENT_REGISTRY``.

    Validates the description length (R2.3) and rejects duplicate names,
    keeping the first record (R2.7). On any rejection an error indication is
    appended to ``_REGISTRATION_ERRORS`` and the registry is left unchanged for
    that name.

    Args:
        name: the unique agent name (registry key).
        description: the Agent_Description read by the Orchestrator for routing;
            must be between one and fourteen words inclusive.
        handler: the agent's ``handle(user_message, history)`` callable.
    """
    words = description.split()
    if not (_MIN_WORDS <= len(words) <= _MAX_WORDS):
        _REGISTRATION_ERRORS.append(
            f"{name}: description must be {_MIN_WORDS}-{_MAX_WORDS} words "
            f"(got {len(words)})"
        )
        return
    if name in AGENT_REGISTRY:
        _REGISTRATION_ERRORS.append(f"duplicate agent name rejected: {name}")
        return
    AGENT_REGISTRY[name] = {"description": description, "handler": handler}


# The five in-scope agent module names. Teammates append their agent module
# names here (e.g. "fee_calculator", "document_verifier") to add new agents
# with no change to the Orchestrator (R2.4).
_IN_SCOPE = [
    "license_advisor",
    "application_intake",
    "compliance_screener",
    "crm_agent",
    "appointment_scheduler",
]


def _load_in_scope_agents() -> None:
    """Import each in-scope agent module and let it register itself.

    A module that fails to import (including one that does not yet exist) is
    skipped; all successfully imported agents are retained and an error
    indication naming the failed agent is recorded (R2.6). This is what lets
    the package initialize even before the individual agent modules (Tasks
    5, 6, 8, 9, 10) are implemented.
    """
    for _mod in _IN_SCOPE:
        try:
            importlib.import_module(f"agents.{_mod}").register_agent()
        except Exception as exc:  # noqa: BLE001 - skip failed agent, keep the rest
            _REGISTRATION_ERRORS.append(f"import failed for {_mod}: {exc}")


_load_in_scope_agents()
