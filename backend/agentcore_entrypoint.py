"""AgentCore Runtime entrypoint for the Ask Gulf single-container image (R18.2, R18.3).

This module is the container's process entrypoint. It wraps the existing FastAPI
application (``main.app``) — the Orchestrator + ``AGENT_REGISTRY`` (License
Advisor, Application Intake, Compliance Screener, CRM Agent, Appointment
Scheduler) + chaining engine + session store — and serves the same
WebSocket/HTTP surface (``/ws/{session_id}``, ``/upload-document``, ``/health``)
that runs locally. Deploying is therefore "the same code": nothing in the
backend source is edited to run under AgentCore.

Deployment-mode switch (R16.3, R18.3), driven entirely by the ``DEPLOYMENT_MODE``
environment variable that the AgentCore runtime / Terraform (``infra/agentcore.tf``)
sets to ``agentcore``:

* ``config.deployment_mode == "agentcore"`` makes ``create_session_store`` select
  :class:`AgentCoreMemorySessionStore` and boto3 clients resolve credentials from
  the AgentCore execution role (no SSO profile, no static keys).
* ``config.deployment_mode == "local"`` selects :class:`InMemorySessionStore` and
  SSO-profile credentials.

The selection logic already lives in :func:`session_store.create_session_store`
and reads ``config.deployment_mode`` — this entrypoint does not re-implement it.

AgentCore Memory client
-----------------------
``create_session_store`` resolves the AgentCore Memory client via
``session_store._default_agentcore_memory_client``, which builds a
``boto3.client("bedrock-agentcore")`` adapter keyed by
``AGENTCORE_MEMORY_ID``. The session store is pre-installed into :mod:`main`
at startup so every request uses the correct backing store immediately.

Run:
    python agentcore_entrypoint.py
or (equivalently) the container CMD invokes ``main()`` below, which binds uvicorn
to the host/port AgentCore expects.
"""

from __future__ import annotations

import logging
import os

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger("ask_gulf.agentcore_entrypoint")

# The port the AgentCore Runtime forwards traffic to. AgentCore's HTTP protocol
# expects the container to listen on 8080 by default; allow an override via env.
DEFAULT_PORT = 8080


def _install_session_store() -> None:
    """Pre-install the deployment-mode-appropriate session store into ``main``."""
    import main
    from config import config
    from session_store import create_session_store

    main._session_store = create_session_store(config)
    logger.info(
        "Session store installed: deployment_mode=%s", config.deployment_mode
    )


def build_app():
    """Return the FastAPI app to serve, after wiring the session store.

    The app object is imported from :mod:`main` unchanged (the same Orchestrator
    + five agents + chaining + session store surface used locally). Importing
    :mod:`agents` here is unnecessary because ``main`` already triggers registry
    initialization on first use; but we touch ``AGENT_REGISTRY`` so that any
    registration errors surface in the container logs at startup.
    """
    _install_session_store()

    import main
    from agents import AGENT_REGISTRY, _REGISTRATION_ERRORS

    logger.info(
        "Ask Gulf runtime starting: %d agents registered (%s).",
        len(AGENT_REGISTRY),
        ", ".join(sorted(AGENT_REGISTRY)) or "none",
    )
    for err in _REGISTRATION_ERRORS:
        logger.warning("agent registration issue: %s", err)

    return main.app


# The module-level ASGI app, so the container can also be launched with an
# external server, e.g. ``uvicorn agentcore_entrypoint:app --host 0.0.0.0``.
app = build_app()


def main() -> None:
    """Run the app under uvicorn, binding to the port AgentCore expects."""
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    logger.info("Serving Ask Gulf runtime on %s:%d", host, port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
