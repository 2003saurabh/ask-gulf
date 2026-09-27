"""Pytest configuration for the Ask Gulf backend test suite.

The backend modules use *absolute* imports (e.g. ``from config import config``,
``import agents``) because they run with ``backend/`` as the working directory
both locally and inside the AgentCore container. Tests, however, are collected
from ``backend/tests/`` and would otherwise not have ``backend/`` on
``sys.path``. This conftest inserts the backend directory (the parent of this
tests package) at the front of ``sys.path`` at collection time so every
absolute import resolves without a package prefix.
"""

from __future__ import annotations

import os
import sys

# backend/tests/conftest.py -> backend/ is one directory up.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

import pytest


@pytest.fixture(autouse=True)
def _reset_aws_client_caches():
    """Reset the process-wide AWS client caches around every test (C3).

    The Bedrock/S3/DynamoDB clients are now built once per process and reused
    (defect C3, R1.6). That reuse must not leak a client built by one test into
    another: a test that patches ``_default_bedrock_client`` with a counting
    factory (e.g. the C3 exploration test) needs the cache empty so it observes
    exactly one construction, and credential-source tests must resolve the
    factory freshly. Clearing the caches before and after each test keeps every
    test deterministic without changing production behaviour.
    """
    import bedrock_client
    import main
    from agents import _dynamo

    bedrock_client.reset_bedrock_client_cache()
    main.reset_s3_client_cache()
    main.reset_last_agent_cache()
    _dynamo.reset_table_cache()
    yield
    bedrock_client.reset_bedrock_client_cache()
    main.reset_s3_client_cache()
    main.reset_last_agent_cache()
    _dynamo.reset_table_cache()
