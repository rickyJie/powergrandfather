"""Shared UTC datetime helpers.

Replaces `datetime.utcnow()` (deprecated in Python 3.12, removed in 3.14).
Two variants preserve current semantics without a big-bang change to how
we store datetimes:

- `now_utc_naive()` — a naive datetime whose value is UTC. Safe drop-in
  replacement for `datetime.utcnow()` at every existing call site that
  hands the result to SQLAlchemy columns declared without `timezone=True`.

- `now_utc_aware()` — a tz-aware UTC datetime. Use where downstream
  callers expect `tzinfo` (e.g., `EventStream.emit` currently uses
  `datetime.now(UTC)` directly). Prefer this at new sites; migrate
  existing ones opportunistically when column types get updated.

Interim policy: keep storage naive (avoids a schema migration) but stop
using the deprecated `utcnow()` call. When we do move to tz-aware
columns, `now_utc_aware()` becomes the default and `now_utc_naive()`
sticks around only for wire formats that require naive strings.
"""
from __future__ import annotations

from datetime import UTC, datetime


def now_utc_naive() -> datetime:
    """Return a naive datetime whose value is the current UTC time.

    Semantically equivalent to `datetime.utcnow()` — the value is UTC,
    but `tzinfo is None`. Use this for SQLAlchemy columns declared
    without `timezone=True` so stored values keep the pre-migration
    format.
    """
    return datetime.now(UTC).replace(tzinfo=None)


def now_utc_aware() -> datetime:
    """Return a tz-aware UTC datetime.

    Use where downstream code (comparisons, serialization, etc.)
    expects `tzinfo` to be set. Mixing this with a naive column value
    in arithmetic will raise `TypeError` — that's intentional; it
    catches naive/aware mistakes at test time instead of masking them.
    """
    return datetime.now(UTC)
