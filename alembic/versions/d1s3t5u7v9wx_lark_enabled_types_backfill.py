"""lark_settings.enabled_types: backfill new_message, auto_needs_review, mission_done

These three notification types were added to `NotificationType` after the
initial `lark_settings` seed migration (a9u2pd3rfqot) shipped, so an
upgraded row's `enabled_types` JSON never had keys for them. LarkSink's
conservative default treats missing keys as `False`, which silently
filtered out every NEW_MESSAGE / AUTO_NEEDS_REVIEW / MISSION_DONE push
from the Lark sink — the "why aren't recent messages showing up in
Lark?" bug.

Backfill is idempotent and non-destructive:
- Only touches the row if it exists (id=1 singleton).
- Only *adds* missing keys as True (respecting the "these three matter
  by default" intent aligned with the fresh-install seed).
- Never overwrites a key the user has explicitly set (so `False` toggles
  stay `False`).

Downgrade removes the same three keys (best-effort — if the user
explicitly toggled them, downgrade still drops the key, but this is a
JSON key removal not a schema change, so the runtime falls back to
"missing = False" and behavior matches pre-migration).

Revision ID: d1s3t5u7v9wx
Revises: a9u2pd3rfqot
Create Date: 2026-08-02
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1s3t5u7v9wx"
down_revision: str | Sequence[str] | None = "a9u2pd3rfqot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_BACKFILL_KEYS = ("new_message", "auto_needs_review", "mission_done")


def upgrade() -> None:
    conn = op.get_bind()
    # Singleton row — nothing to do on a fresh install (a9u2pd3rfqot's
    # INSERT OR IGNORE already fired). This block only runs meaningfully
    # for upgraders whose row pre-dates the new NotificationType values.
    row = conn.execute(
        sa.text("SELECT enabled_types FROM lark_settings WHERE id = 1")
    ).first()
    if row is None:
        return
    raw = row[0]
    # SQLite JSON column can round-trip as str OR as parsed dict depending
    # on driver / cursor mode; handle both defensively.
    if isinstance(raw, str):
        try:
            current = json.loads(raw or "{}")
        except json.JSONDecodeError:
            current = {}
    elif isinstance(raw, dict):
        current = dict(raw)
    else:
        current = {}

    changed = False
    for k in _BACKFILL_KEYS:
        if k not in current:
            current[k] = True
            changed = True

    if changed:
        conn.execute(
            sa.text(
                "UPDATE lark_settings SET enabled_types = :et, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
            ).bindparams(et=json.dumps(current))
        )


def downgrade() -> None:
    conn = op.get_bind()
    row = conn.execute(
        sa.text("SELECT enabled_types FROM lark_settings WHERE id = 1")
    ).first()
    if row is None:
        return
    raw = row[0]
    if isinstance(raw, str):
        try:
            current = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return
    elif isinstance(raw, dict):
        current = dict(raw)
    else:
        return

    changed = False
    for k in _BACKFILL_KEYS:
        if k in current:
            current.pop(k)
            changed = True

    if changed:
        conn.execute(
            sa.text(
                "UPDATE lark_settings SET enabled_types = :et, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
            ).bindparams(et=json.dumps(current))
        )
