"""Session-store abstraction for Ask Gulf (Design Component 7; R16.2, R16.3, R18.3).

Conversation/session state is decoupled from its backing store behind a small
``SessionStore`` protocol. The concrete store is selected by
``Config.deployment_mode``:

* ``"local"``     -> ``InMemorySessionStore`` (a process-local dict).
* ``"agentcore"`` -> ``AgentCoreMemorySessionStore`` (backed by AgentCore Memory).

Each processed user message appends exactly one user turn and one assistant
turn (see ``append_message_turns``), matching the runtime's history-uniformity
guarantee (R3 / R18.3).

AgentCore Memory SDK note
-------------------------
The deployed (``agentcore``) store talks to the AgentCore Memory *data plane*
via ``boto3.client("bedrock-agentcore")`` (the control plane service name is
``bedrock-agentcore-control``). The exact data-plane operation names and
request/response shapes for events (``create_event`` / ``list_events`` /
``retrieve``) vary across botocore versions, so ``_AgentCoreMemoryAdapter``
below discovers the available operations defensively (``getattr`` / ``hasattr``
+ ``try/except``) and raises a clear, logged error when a needed capability is
missing rather than crashing obscurely. The event payload/response schema may
need adjustment to match the botocore version baked into the runtime image.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from config import Config, config as default_config


logger = logging.getLogger("ask_gulf.session_store")


# A single conversation turn is a ``{"role": str, "content": str}`` dict, matching
# the history shape consumed by the Orchestrator and agent handlers.
Turn = dict


@runtime_checkable
class SessionStore(Protocol):
    """Interface for reading and appending conversation history.

    Implementations MUST be safe to call with an unseen ``session_id`` (an
    empty history is returned rather than raising).
    """

    def get_history(self, session_id: str) -> list[dict]:
        """Return the ordered list of turns for ``session_id`` (empty if none)."""
        ...

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        """Append a single ``{"role", "content"}`` turn to the session history."""
        ...


class InMemorySessionStore:
    """Local-first store: a plain dict keyed by ``session_id`` (R18.1).

    History lives only for the lifetime of the process. Returned histories are
    copies so callers cannot mutate the stored list in place.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, list[dict]] = {}

    def get_history(self, session_id: str) -> list[dict]:
        return list(self._sessions.get(session_id, []))

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        self._sessions.setdefault(session_id, []).append(
            {"role": role, "content": content}
        )


class AgentCoreMemorySessionStore:
    """Deployed store backed by AgentCore Memory (R16.3, R18.3).

    When running inside AgentCore, conversation state is held in AgentCore
    Memory rather than a process-local dict. Credentials are resolved from the
    AgentCore execution role by the underlying client (no secrets in source,
    R16.1/R16.3).

    The AgentCore Memory client is injected so the store is testable and so the
    runtime controls credential/session wiring. The expected client exposes:

    * ``list_events(session_id) -> list[{"role", "content"}]``
    * ``create_event(session_id, role, content) -> None``
    """

    def __init__(self, memory_client) -> None:
        if memory_client is None:
            raise ValueError(
                "AgentCoreMemorySessionStore requires an AgentCore Memory client"
            )
        self._client = memory_client

    def get_history(self, session_id: str) -> list[dict]:
        events = self._client.list_events(session_id) or []
        return [
            {"role": e["role"], "content": e["content"]}
            for e in events
        ]

    def append_turn(self, session_id: str, role: str, content: str) -> None:
        self._client.create_event(session_id, role, content)


def append_message_turns(
    store: SessionStore,
    session_id: str,
    user_content: str,
    assistant_content: str,
) -> None:
    """Append exactly one user turn and one assistant turn for a processed message.

    Every processed message contributes precisely two turns to the history in
    order (user first, then assistant), keeping history uniform regardless of
    how many agents were invoked for the message (R18.3).
    """
    store.append_turn(session_id, "user", user_content)
    store.append_turn(session_id, "assistant", assistant_content)


def create_session_store(
    cfg: Config | None = None,
    *,
    memory_client=None,
) -> SessionStore:
    """Select the session store from ``Config.deployment_mode``.

    * ``"local"``     -> :class:`InMemorySessionStore`.
    * ``"agentcore"`` -> :class:`AgentCoreMemorySessionStore` (requires a
      ``memory_client``; when omitted, one is resolved lazily so importing this
      module never forces an AgentCore dependency during local development).

    Any other mode value is an error indication rather than a silent default.
    """
    cfg = cfg if cfg is not None else default_config
    mode = cfg.deployment_mode

    if mode == "local":
        return InMemorySessionStore()

    if mode == "agentcore":
        if memory_client is None:
            memory_client = _default_agentcore_memory_client(cfg)
        return AgentCoreMemorySessionStore(memory_client)

    raise ValueError(
        f"unknown deployment_mode {mode!r}; expected 'local' or 'agentcore'"
    )


class _AgentCoreMemoryAdapter:
    """Adapter over the AgentCore Memory data-plane boto3 client (R16.3, R18.3).

    Uses the exact verified API shapes for ap-south-1:
    - create_event: memoryId + actorId + sessionId + payload list of
      {"conversational": {"role": <role>, "content": {"text": <text>}}}
    - list_events: memoryId + sessionId + actorId + includePayloads=True,
      response: {"events": [{"payload": [{"conversational": {"role":..., "content":{"text":...}}}]}]}
    """

    def __init__(self, client: Any, memory_id: str) -> None:
        if not memory_id:
            raise ValueError(
                "AGENTCORE_MEMORY_ID is empty; set it to the AgentCore Memory "
                "resource id so the deployed session store can read/write events."
            )
        self._client = client
        self._memory_id = memory_id

    def create_event(self, session_id: str, role: str, content: str) -> None:
        """Persist one conversation turn as an AgentCore Memory conversational event."""
        try:
            self._client.create_event(
                memoryId=self._memory_id,
                actorId=session_id,
                sessionId=session_id,
                payload=[{
                    "conversational": {
                        "role": role,
                        "content": {"text": content},
                    }
                }],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "AgentCore Memory create_event failed for session=%s", session_id
            )
            raise RuntimeError(f"AgentCore Memory create_event failed: {exc}") from exc

    def list_events(self, session_id: str) -> list[dict]:
        """Return this session's turns as [{"role", "content"}, ...]."""
        try:
            result = self._client.list_events(
                memoryId=self._memory_id,
                sessionId=session_id,
                actorId=session_id,
                includePayloads=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "AgentCore Memory list_events failed for session=%s", session_id
            )
            raise RuntimeError(f"AgentCore Memory list_events failed: {exc}") from exc

        return _normalise_events(result)


def _normalise_events(result: Any) -> list[dict]:
    """Coerce a list_events response into [{"role", "content"}, ...].

    Response shape: {"events": [{"payload": [{"conversational": {"role": ..., "content": {"text": ...}}}]}]}
    Handles missing/empty payloads gracefully — returns empty list rather than raising.
    """
    turns: list[dict] = []
    events = result.get("events") or [] if isinstance(result, dict) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        for payload_item in (event.get("payload") or []):
            if not isinstance(payload_item, dict):
                continue
            conv = payload_item.get("conversational")
            if not isinstance(conv, dict):
                continue
            role = conv.get("role")
            content_obj = conv.get("content")
            text = content_obj.get("text", "") if isinstance(content_obj, dict) else str(content_obj or "")
            if role and text:
                turns.append({"role": role, "content": text})
    return turns


def _default_agentcore_memory_client(cfg: Config):
    """Resolve the AgentCore Memory data-plane client for the deployed runtime.

    Builds a ``boto3.client("bedrock-agentcore")`` (the Memory *data plane*; the
    control plane is ``bedrock-agentcore-control``) using ambient credentials —
    the SSO profile locally when ``cfg.aws_sso_profile`` is set, otherwise the
    default credential chain (the AgentCore execution role in the runtime),
    mirroring :func:`bedrock_client._default_bedrock_client` (R16). The returned
    :class:`_AgentCoreMemoryAdapter` exposes the ``list_events`` /
    ``create_event`` methods the store expects, keyed by ``cfg.agentcore_memory_id``
    (``AGENTCORE_MEMORY_ID``). Raises a clear error when the memory id is empty
    so misconfiguration is loud, not silent.
    """
    memory_id = cfg.agentcore_memory_id
    if not memory_id:
        raise ValueError(
            "AGENTCORE_MEMORY_ID is not set; the AgentCore Memory session store "
            "requires the Memory resource id. Set the AGENTCORE_MEMORY_ID "
            "environment variable (or pass memory_client explicitly)."
        )

    import boto3  # local import keeps the module import-safe without AWS

    if cfg.aws_sso_profile:
        session = boto3.Session(profile_name=cfg.aws_sso_profile)
        client = session.client("bedrock-agentcore", region_name=cfg.aws_region)
    else:
        client = boto3.client("bedrock-agentcore", region_name=cfg.aws_region)

    return _AgentCoreMemoryAdapter(client, memory_id)
