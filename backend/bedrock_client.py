"""Agent response contract and the shared Bedrock invocation helper.

This module centralises three primitives used across the runtime and every
agent (design §3, "Agent Handler Contract & Shared Bedrock Helper"):

* ``AgentResponse`` — the uniform dict shape every agent handler returns
  (R3.3, R3.4, R3.5, R9.1).
* ``invoke_agent`` — times a Bedrock ``invoke_model`` call, and on error or
  timeout degrades to a predefined fallback response with
  ``tools_used == ["fallback-mode"]`` (R10.1, R10.2). Agents use the Haiku
  model for speed (R3.6).
* ``validate_response`` — a guard the runtime applies to a handler's response;
  it rejects contract violations and returns an error indication naming the
  violation, leaving the conversation history unchanged (R3.7).

No AWS credentials appear here; boto3 resolves them from the SSO profile
(local) or the AgentCore execution role (deployed) per config (R16).
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from typing import Any, Callable, Optional, TypedDict

from config import config

# Small slack added to the per-call wall-clock guard so a call that finishes
# right at its budget is not spuriously cut off, while still guaranteeing the
# agent degrades no more than ~1s after the budget (C4, R1.7).
_BUDGET_SLACK_S: float = 0.25

# A process-wide daemon executor used to run the blocking Bedrock invoke under a
# wall-clock guard. A daemon thread does not block interpreter exit, so an
# orphaned stuck call (whose result we abandoned on timeout) never delays the
# process. Threads are reused across calls.
_invoke_executor = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="bedrock-invoke"
)

# Model id defaults. Agents default to Haiku for faster responses (R3.6); the
# Orchestrator passes the Sonnet id explicitly for routing.
HAIKU = config.haiku_model_id
SONNET = config.sonnet_model_id


class AgentResponse(TypedDict, total=False):
    """The uniform response contract returned by every agent handler.

    Required fields:
        text: non-empty user-facing string (R3.3).
        tools_used: list of AWS service identifiers, e.g. ["bedrock"];
            ["fallback-mode"] on fallback (R3.3, R10.2).
        time_ms: elapsed milliseconds from invocation start to completion,
            a non-negative integer (R3.4).

    Optional fields:
        structured_data: structured output produced by the agent (R3.5).
        chain_next: name of the next agent the runtime should invoke (R9.1).
        is_recommendation: additive, non-user-visible flag the License Advisor
            sets to ``True`` when a turn is a genuine recommendation (a fixed
            package with its AED cost, or a custom quote). The CRM auto-fire
            gate consumes it so it fires on a real recommendation instead of on
            every License Advisor turn (latency-trace-fix defect C6).
    """

    text: str
    tools_used: list[str]
    time_ms: int
    structured_data: dict[str, Any]
    chain_next: str
    is_recommendation: bool


class ContractViolation(Exception):
    """Raised/returned when an ``AgentResponse`` violates the contract (R3.7)."""


# The required fields and their expected python types.
_REQUIRED_FIELDS: tuple[str, ...] = ("text", "tools_used", "time_ms")


def _elapsed_ms(start: float) -> int:
    """Return whole milliseconds elapsed since ``start`` (never negative)."""
    elapsed = int((time.perf_counter() - start) * 1000)
    return elapsed if elapsed >= 0 else 0


class BedrockError(Exception):
    """Raised when a Bedrock invocation fails for a non-timeout reason."""


def _default_bedrock_client():
    """Create a Bedrock Runtime client using ambient (non-secret) credentials.

    Imported lazily so that unit tests and non-Bedrock code paths do not
    require boto3 or live AWS credentials.

    This factory is *always* the single point at which the credential source is
    resolved (SSO named profile locally when ``config.aws_sso_profile`` is set,
    otherwise the default credential chain). The process-wide cache in
    :func:`get_bedrock_client` calls through here exactly once, so the
    credential-source behaviour (3.13) is preserved unchanged.
    """
    import boto3  # local import keeps the module import-safe without AWS

    # The hard per-call timeout guarantee (C4, R1.7) comes from the wall-clock
    # guard in :func:`invoke_agent` (``future.result(timeout=...)``), which cuts
    # a stuck call off regardless of any client-side socket configuration. We
    # therefore keep the client construction signature minimal here so the
    # credential source (SSO named profile vs default chain, 3.13) is the only
    # thing this factory decides.
    if config.aws_sso_profile:
        session = boto3.Session(profile_name=config.aws_sso_profile)
        return session.client("bedrock-runtime", region_name=config.aws_region)
    return boto3.client("bedrock-runtime", region_name=config.aws_region)


# --------------------------------------------------------------------------- #
# Process-wide, thread-safe Bedrock Runtime client reuse (C3)
# --------------------------------------------------------------------------- #
#
# Constructing a boto3 client on every agent invocation causes needless client
# churn (defect C3, R1.6). A bedrock-runtime low-level client is thread-safe
# once created, so it is built ONCE per process, lazily, behind a lock and
# reused across all calls. Double-checked locking keeps concurrent first-use
# safe without paying the lock cost on the hot path after construction.
#
# The cache calls through :func:`_default_bedrock_client`, so the credential
# source (SSO vs default chain) is resolved exactly as before (3.13); only the
# reuse is new. Tests that inject a client never touch this singleton.

_bedrock_client_singleton: Optional[Any] = None
_bedrock_client_lock = threading.Lock()


def get_bedrock_client() -> Any:
    """Return the process-wide Bedrock Runtime client, building it once (C3).

    Lazily constructs the client via :func:`_default_bedrock_client` on first
    use and reuses it for every subsequent call, so the factory (and thus the
    credential resolution) runs exactly once per process. Thread-safe via
    double-checked locking so concurrent first-use never races or builds twice.
    """
    global _bedrock_client_singleton
    client = _bedrock_client_singleton
    if client is None:
        with _bedrock_client_lock:
            client = _bedrock_client_singleton
            if client is None:
                client = _default_bedrock_client()
                _bedrock_client_singleton = client
    return client


def reset_bedrock_client_cache() -> None:
    """Clear the cached Bedrock client (test seam; not used in production).

    Lets tests exercise first-construction behaviour deterministically without
    leaking a client built by an earlier test into the next one.
    """
    global _bedrock_client_singleton
    with _bedrock_client_lock:
        _bedrock_client_singleton = None


def _build_messages(
    user_message: str, history: Optional[list[dict]]
) -> list[dict]:
    """Build the Anthropic messages array from history plus the new message."""
    messages: list[dict] = []
    for turn in history or []:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": user_message})
    return messages


def _bedrock_invoke(
    *,
    model_id: str,
    system_prompt: str,
    user_message: str,
    history: Optional[list[dict]],
    timeout_s: float,
    client: Any,
    max_tokens: int,
) -> str:
    """Invoke Bedrock ``invoke_model`` and return the response text.

    Uses anthropic_version ``bedrock-2023-05-31`` from config. Raises
    ``BedrockError`` on any failure so the caller can degrade to fallback.
    """
    body = {
        "anthropic_version": config.anthropic_version,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": _build_messages(user_message, history),
    }
    try:
        result = client.invoke_model(
            modelId=model_id,
            body=json.dumps(body),
        )
    except Exception as exc:  # noqa: BLE001 - normalise all boto/network errors
        # Surface timeouts distinctly so callers can treat them as R10.1.
        if _looks_like_timeout(exc):
            raise TimeoutError(str(exc)) from exc
        raise BedrockError(str(exc)) from exc

    return _extract_text(result)


def _looks_like_timeout(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    return isinstance(exc, TimeoutError) or "timeout" in name or "readtimeout" in name


def _extract_text(result: Any) -> str:
    """Pull the assistant text out of a Bedrock invoke_model response."""
    raw = result.get("body") if isinstance(result, dict) else None
    if raw is None:
        raise BedrockError("bedrock response missing body")
    payload = raw.read() if hasattr(raw, "read") else raw
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    if isinstance(payload, str):
        payload = json.loads(payload)
    # Anthropic messages API returns {"content": [{"type":"text","text":...}]}
    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "".join(parts).strip()
        if text:
            return text
    raise BedrockError("bedrock response contained no text")


def invoke_agent(
    *,
    system_prompt: str,
    user_message: str,
    history: Optional[list[dict]] = None,
    model_id: str = HAIKU,
    tools_used: list[str],
    fallback_text: str,
    timeout_s: float = 10.0,
    bedrock_client: Optional[Any] = None,
    max_tokens: int = 1024,
) -> AgentResponse:
    """Invoke Bedrock for an agent, timing the call and handling fallback.

    Times the call and sets ``time_ms`` from invocation start to completion
    (R3.4). Invokes Bedrock ``invoke_model`` with anthropic_version
    ``bedrock-2023-05-31`` (R3.6 default Haiku). On any Bedrock error or
    timeout, returns ``fallback_text`` with ``tools_used=["fallback-mode"]``
    (R10.1, R10.2).

    Args:
        system_prompt: the agent's system prompt.
        user_message: the current user message.
        history: prior conversation turns ({"role","content"}), optional.
        model_id: Bedrock model id (defaults to Haiku for speed, R3.6).
        tools_used: AWS service identifiers to report on success.
        fallback_text: the agent's predefined fallback response (R10.3).
        timeout_s: per-call timeout budget in seconds.
        bedrock_client: optional injected client (used by tests/mocks).
        max_tokens: response token cap.

    Returns:
        An ``AgentResponse`` dict satisfying the contract.
    """
    start = time.perf_counter()
    try:
        # Resolve the client BEFORE submitting to the executor so the C3
        # process-wide client is constructed exactly once (an injected client is
        # honoured as-is and never touches the singleton). Doing this on the
        # calling thread keeps the counting-factory (C3) seeing one construction.
        client = bedrock_client if bedrock_client is not None else get_bedrock_client()

        # R1.7 (C4): enforce ``timeout_s`` with a wall-clock guard. Run the
        # blocking invoke on the shared daemon executor and wait only up to the
        # budget plus a small slack. On timeout we abandon the (possibly stuck)
        # worker and degrade immediately -- a daemon thread never blocks exit.
        future = _invoke_executor.submit(
            _bedrock_invoke,
            model_id=model_id,
            system_prompt=system_prompt,
            user_message=user_message,
            history=history,
            timeout_s=timeout_s,
            client=client,
            max_tokens=max_tokens,
        )
        text = future.result(timeout=timeout_s + _BUDGET_SLACK_S)
        return AgentResponse(
            text=text,
            tools_used=list(tools_used),
            time_ms=_elapsed_ms(start),
        )
    except Exception:  # noqa: BLE001 - any failure (client build/invoke/timeout) degrades
        # R10.1/R10.2: never stall. Client construction (e.g. botocore
        # ProfileNotFound / NoCredentialsError), invocation
        # (BedrockError/TimeoutError), or the wall-clock guard firing
        # (concurrent.futures.TimeoutError) all degrade to the fallback while
        # preserving the timing behaviour. BaseException (KeyboardInterrupt/
        # SystemExit) is intentionally NOT caught and propagates.
        return AgentResponse(
            text=fallback_text,
            tools_used=["fallback-mode"],
            time_ms=_elapsed_ms(start),
        )


def validate_response(resp: Any) -> Optional[str]:
    """Validate an agent response against the contract (R3.7).

    Returns ``None`` when the response is valid. Otherwise returns a
    human-readable error indication naming the specific violation. The caller
    (runtime) uses a non-None return to reject the response and leave the
    conversation history unchanged.

    Contract:
        text: present, a str, and non-empty (R3.3).
        tools_used: present and a list of strings (R3.3).
        time_ms: present, an int (not bool), and >= 0 (R3.4).
        structured_data (optional): a dict when present (R3.5).
        chain_next (optional): a non-empty str when present (R9.1).
    """
    if not isinstance(resp, dict):
        return f"response is not a mapping (got {type(resp).__name__})"

    for missing in (f for f in _REQUIRED_FIELDS if f not in resp):
        return f"missing required field: {missing}"

    text = resp["text"]
    if not isinstance(text, str):
        return f"field 'text' must be a string (got {type(text).__name__})"
    if text.strip() == "":
        return "field 'text' must be a non-empty string"

    tools_used = resp["tools_used"]
    if not isinstance(tools_used, list):
        return f"field 'tools_used' must be a list (got {type(tools_used).__name__})"
    for i, tool in enumerate(tools_used):
        if not isinstance(tool, str):
            return f"field 'tools_used[{i}]' must be a string (got {type(tool).__name__})"

    time_ms = resp["time_ms"]
    # bool is a subclass of int; reject it explicitly.
    if isinstance(time_ms, bool) or not isinstance(time_ms, int):
        return f"field 'time_ms' must be an integer (got {type(time_ms).__name__})"
    if time_ms < 0:
        return f"field 'time_ms' must be >= 0 (got {time_ms})"

    if "structured_data" in resp and not isinstance(resp["structured_data"], dict):
        return (
            "field 'structured_data' must be a mapping when present "
            f"(got {type(resp['structured_data']).__name__})"
        )

    if "chain_next" in resp:
        chain_next = resp["chain_next"]
        if not isinstance(chain_next, str) or chain_next.strip() == "":
            return "field 'chain_next' must be a non-empty string when present"

    return None
