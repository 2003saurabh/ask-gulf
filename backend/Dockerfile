# syntax=docker/dockerfile:1
###############################################################################
# Ask Gulf — single-container image for Amazon Bedrock AgentCore Runtime.
#
# This image packages the whole in-scope backend as ONE container (design
# "Deployment / AgentCore Runtime", R18.2): the FastAPI app (main.app) with the
# Orchestrator, the AGENT_REGISTRY + five agents (License Advisor, Application
# Intake, Compliance Screener, CRM Agent, Appointment Scheduler), the chaining
# engine, the CRM auto-fire hook, and the session-store abstraction.
#
# The container runs `agentcore_entrypoint.py`, which wraps main.app and serves
# the same WebSocket/HTTP surface used locally (/ws/{session_id},
# /upload-document, /health).
#
# Deployment-mode switch (R16.3, R18.3): the mode is chosen purely by the
# DEPLOYMENT_MODE env var (config.py reads it), so NO source edit is needed to
# switch stores/credentials. Terraform (infra/agentcore.tf) sets
# DEPLOYMENT_MODE=agentcore on the deployed runtime, selecting the AgentCore
# Memory session store + execution-role credentials. The default below is
# `agentcore` because this image is the deployment artifact; running the same
# image locally with `-e DEPLOYMENT_MODE=local` gives the in-memory store.
#
# No AWS credentials are baked into the image (R16.1): locally boto3 uses the
# SSO profile, in the runtime it uses the AgentCore execution role.
###############################################################################

FROM python:3.12-slim AS base

# Predictable, log-friendly Python behavior in a container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so the layer is cached across code-only changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the backend source (the build context is the backend/ directory).
COPY . .

# AgentCore's HTTP protocol forwards to this port; keep it overridable via env.
ENV PORT=8080 \
    HOST=0.0.0.0 \
    DEPLOYMENT_MODE=agentcore

EXPOSE 8080

# Container-native health check hitting the runtime's /health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
url='http://127.0.0.1:%s/health' % os.environ.get('PORT','8080'); \
sys.exit(0 if urllib.request.urlopen(url, timeout=4).status == 200 else 1)"

# Launch the app via the AgentCore entrypoint (uvicorn under the hood). Using
# the module runner keeps HOST/PORT/DEPLOYMENT_MODE env-driven.
CMD ["python", "agentcore_entrypoint.py"]
