"""Shared, thread-safe DynamoDB Table reuse for the lead/application agents (C3).

Both the CRM_Agent and the Application_Intake agent persist to the same
DynamoDB table (``config.dynamodb_table``). Building a fresh boto3
``resource``/``Table`` on every write causes needless client churn (defect C3,
R1.6).

boto3 ``Session``/``resource``/``Table`` objects are **not** thread-safe and
must not be shared across threads, so the reuse here is per-thread: each thread
lazily builds its own ``Table`` on first use via :func:`_build_table` and reuses
it for subsequent writes on that thread. This eliminates the per-call churn
while never sharing a non-thread-safe resource across threads (design §"Reused
AWS clients (C3)").

The credential source is resolved exactly as before — an SSO named profile
locally when ``config.aws_sso_profile`` is set, otherwise the default
credential chain — so 3.13 is preserved; only the reuse is new. Callers that
inject their own ``table`` (tests/mocks) never touch this cache.
"""

from __future__ import annotations

import threading
from typing import Any

from config import config

# Per-thread cache of Table resources, keyed by table name. A resource/Table is
# reused within the thread that built it and never shared across threads.
_thread_local = threading.local()


def _build_table(table_name: str) -> Any:
    """Build a DynamoDB ``Table`` resource for ``table_name``.

    boto3 is imported lazily so importing this module never forces a boto3
    dependency or live AWS credentials during local development / tests. The
    credential source mirrors the other client factories (SSO profile locally,
    default chain deployed) so 3.13 is unchanged.
    """
    import boto3  # local import keeps the module import-safe without AWS

    if config.aws_sso_profile:
        session = boto3.Session(profile_name=config.aws_sso_profile)
        resource = session.resource("dynamodb", region_name=config.aws_region)
    else:
        resource = boto3.resource("dynamodb", region_name=config.aws_region)
    return resource.Table(table_name)


def get_table(table_name: str) -> Any:
    """Return a per-thread cached DynamoDB ``Table`` for ``table_name`` (C3).

    Lazily builds the ``Table`` on the calling thread's first use and reuses it
    for later calls on that thread, avoiding per-call construction. Each thread
    keeps its own resource so a non-thread-safe boto3 object is never shared.
    """
    cache = getattr(_thread_local, "tables", None)
    if cache is None:
        cache = {}
        _thread_local.tables = cache
    table = cache.get(table_name)
    if table is None:
        table = _build_table(table_name)
        cache[table_name] = table
    return table


def reset_table_cache() -> None:
    """Clear this thread's cached Table resources (test seam; not production)."""
    _thread_local.tables = {}
