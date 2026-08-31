"""Shared serialization helpers for API responses.

The database stores datetimes as naive (SQLite limitation in our schema; treating
them as UTC by convention). API responses must emit explicit timezone so external
consumers (frontend, third-party) can disambiguate.
"""
from __future__ import annotations

from datetime import UTC, datetime


def iso_utc(dt: datetime | None) -> str | None:
    """Serialize a (possibly naive) datetime as an ISO-8601 UTC string ending in '+00:00'.

    - None  → None
    - naive → assume UTC, append +00:00
    - aware → convert to UTC, emit isoformat()
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.isoformat()
