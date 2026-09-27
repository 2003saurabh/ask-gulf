"""Mock data package for Ask Gulf.

Holds the demo JSON files (``watchlist.json``, ``calendar.json``,
``applications.json``, ``fee_schedule.json``) and the :mod:`mock_data.loader`
module that reads them and seeds DynamoDB/S3 (design "Mock Data JSON Schemas";
R14).
"""

from mock_data.loader import (  # noqa: F401 - re-export the public loader API
    load_applications,
    load_calendar,
    load_fee_schedule,
    load_watchlist,
    seed_all,
    seed_dynamodb,
    seed_s3,
)
