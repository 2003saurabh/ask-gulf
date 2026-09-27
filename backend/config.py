"""Configuration / environment model for Ask Gulf.

No secrets are stored in source (R16.1, R16.4). Values are read from the
environment with the design-specified defaults. Local development resolves AWS
credentials from an SSO profile; the deployed environment uses the AgentCore
execution role (R16.2, R16.3, R18).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_optional(key: str) -> str | None:
    return os.environ.get(key)


@dataclass
class Config:
    """Runtime configuration for the Ask Gulf system.

    Fields and defaults match the design's Configuration / Environment Model.
    No hardcoded AWS credentials appear here (R16.1).
    """

    aws_region: str = field(default_factory=lambda: _env("AWS_REGION", "ap-south-1"))
    sonnet_model_id: str = field(
        default_factory=lambda: _env(
            "SONNET_MODEL_ID", "apac.anthropic.claude-3-5-sonnet-20241022-v2:0"
        )
    )
    haiku_model_id: str = field(
        default_factory=lambda: _env(
            "HAIKU_MODEL_ID", "apac.anthropic.claude-3-haiku-20240307-v1:0"
        )
    )
    anthropic_version: str = field(
        default_factory=lambda: _env("ANTHROPIC_VERSION", "bedrock-2023-05-31")
    )
    dynamodb_table: str = field(
        default_factory=lambda: _env("DYNAMODB_TABLE", "ask-gulf-leads")
    )
    s3_bucket: str = field(
        default_factory=lambda: _env("S3_BUCKET", "ask-gulf-documents")
    )
    # AgentCore Memory resource id for the deployed session store (R16.3, R18.3).
    # Empty locally; supplied via env in the AgentCore Runtime container.
    agentcore_memory_id: str = field(
        default_factory=lambda: _env("AGENTCORE_MEMORY_ID", "")
    )
    # Local dev only (R16.2). Resolved from the environment; never a secret.
    aws_sso_profile: str | None = field(default_factory=lambda: _env_optional("AWS_SSO_PROFILE"))
    # "local" | "agentcore" (R18).
    deployment_mode: str = field(
        default_factory=lambda: _env("DEPLOYMENT_MODE", "local")
    )
    default_agent: str = field(
        default_factory=lambda: _env("DEFAULT_AGENT", "license_advisor")
    )


# Module-level singleton for convenient import across the runtime.
config = Config()
