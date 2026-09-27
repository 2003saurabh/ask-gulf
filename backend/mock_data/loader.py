"""Mock data loader and AWS seed routine for Ask Gulf (design "Mock Data JSON Schemas"; R14).

This module owns the ``backend/mock_data/*.json`` files and provides:

* Reader functions used by the agents at request time:
    - :func:`load_watchlist`  -> consumed by the Compliance Screener (R6, R14.2).
    - :func:`load_calendar`   -> consumed by the Appointment Scheduler (R8, R14.3).
    - :func:`load_fee_schedule` and :func:`load_applications` for completeness
      (R14.1); ``fee_schedule.json`` exists so the future chain has data.

* A seed routine (:func:`seed_all`) that loads the relevant mock data into
  DynamoDB (``ask-gulf-leads``) and S3 (``ask-gulf-documents``) so a freshly
  provisioned environment has realistic data to operate on (R14.6).

The reader functions never require AWS; they read local JSON so agents run
local-first. The seed routine imports boto3 lazily and resolves the table and
bucket names from :mod:`config` (no hardcoded credentials, R16.1).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import Config, config as default_config

# Directory holding the mock JSON files (this module's own directory).
MOCK_DATA_DIR = Path(__file__).resolve().parent

WATCHLIST_FILE = "watchlist.json"
CALENDAR_FILE = "calendar.json"
FEE_SCHEDULE_FILE = "fee_schedule.json"
APPLICATIONS_FILE = "applications.json"


def _read_json(filename: str) -> Any:
    """Read and parse a mock data file from :data:`MOCK_DATA_DIR`.

    Raises ``FileNotFoundError`` if the file is missing and
    ``json.JSONDecodeError`` if it does not parse, so callers/tests surface a
    clear failure rather than silently degrading.
    """
    path = MOCK_DATA_DIR / filename
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


# --- Reader functions used by the agents --------------------------------------


@lru_cache(maxsize=None)
def load_watchlist() -> list[dict]:
    """Return the sanctions/PEP watchlist entries (R6, R14.2).

    Each entry has ``name``, ``identifiers`` (list), ``list``, and ``reason``.
    Cached because the watchlist is read-only at runtime.
    """
    return _read_json(WATCHLIST_FILE)


@lru_cache(maxsize=None)
def load_calendar() -> list[dict]:
    """Return the appointment calendar slots (R8, R14.3).

    Each slot has ``slot_id``, ``date``, ``time``, ``rm_name``, and ``booked``.
    """
    return _read_json(CALENDAR_FILE)


@lru_cache(maxsize=None)
def load_fee_schedule() -> dict:
    """Return the fee schedule used by the later Fee Calculator (R14.1)."""
    return _read_json(FEE_SCHEDULE_FILE)


@lru_cache(maxsize=None)
def load_applications() -> list[dict]:
    """Return the seed/sample draft application records (R14.1)."""
    return _read_json(APPLICATIONS_FILE)


def clear_cache() -> None:
    """Clear cached reads (useful for tests that rewrite mock files)."""
    load_watchlist.cache_clear()
    load_calendar.cache_clear()
    load_fee_schedule.cache_clear()
    load_applications.cache_clear()


# --- AWS seed routine ---------------------------------------------------------


def _resource(service: str, cfg: Config):
    """Create a boto3 resource resolving ambient (non-secret) credentials.

    Imported lazily so importing this module never forces a boto3 dependency
    or live AWS credentials during local development / tests.
    """
    import boto3  # local import keeps the module import-safe without AWS

    if cfg.aws_sso_profile:
        session = boto3.Session(profile_name=cfg.aws_sso_profile)
        return session.resource(service, region_name=cfg.aws_region)
    return boto3.resource(service, region_name=cfg.aws_region)


def seed_dynamodb(cfg: Config | None = None, *, table=None) -> int:
    """Seed the ``ask-gulf-leads`` table with sample draft applications (R14.6).

    Draft application records mirror the design's single-table shape: a ``pk``
    of the ``application_id`` and ``entity_type == "application"``. Returns the
    number of records written.

    Args:
        cfg: configuration providing ``dynamodb_table`` and region/profile.
        table: an optional injected DynamoDB Table resource (for tests/mocks).
    """
    cfg = cfg if cfg is not None else default_config
    if table is None:
        table = _resource("dynamodb", cfg).Table(cfg.dynamodb_table)

    applications = load_applications()
    written = 0
    for app in applications:
        item = {
            "pk": app["application_id"],
            "entity_type": "application",
            **app,
        }
        table.put_item(Item=item)
        written += 1
    return written


def seed_s3(cfg: Config | None = None, *, s3=None) -> list[str]:
    """Upload the mock data files to the ``ask-gulf-documents`` bucket (R14.6).

    The watchlist and calendar are uploaded under a ``mock_data/`` prefix so the
    deployed environment can read screening/scheduling data from S3 (design §4:
    the screener/scheduler report ``["s3"]``). Returns the list of object keys
    written.

    Args:
        cfg: configuration providing ``s3_bucket`` and region/profile.
        s3: an optional injected S3 resource (for tests/mocks).
    """
    cfg = cfg if cfg is not None else default_config
    if s3 is None:
        s3 = _resource("s3", cfg)
    bucket = s3.Bucket(cfg.s3_bucket)

    keys: list[str] = []
    for filename in (WATCHLIST_FILE, CALENDAR_FILE, FEE_SCHEDULE_FILE, APPLICATIONS_FILE):
        key = f"mock_data/{filename}"
        body = (MOCK_DATA_DIR / filename).read_bytes()
        bucket.put_object(Key=key, Body=body, ContentType="application/json")
        keys.append(key)
    return keys


def seed_all(cfg: Config | None = None, *, table=None, s3=None) -> dict[str, Any]:
    """Load relevant mock data into both DynamoDB and S3 (R14.6).

    Returns a summary ``{"dynamodb_records": int, "s3_keys": list[str]}`` so a
    caller (or CLI) can report what was seeded.
    """
    cfg = cfg if cfg is not None else default_config
    records = seed_dynamodb(cfg, table=table)
    keys = seed_s3(cfg, s3=s3)
    return {"dynamodb_records": records, "s3_keys": keys}


if __name__ == "__main__":  # pragma: no cover - manual seed entrypoint
    summary = seed_all()
    print(
        f"Seeded {summary['dynamodb_records']} DynamoDB records and "
        f"{len(summary['s3_keys'])} S3 objects: {summary['s3_keys']}"
    )
