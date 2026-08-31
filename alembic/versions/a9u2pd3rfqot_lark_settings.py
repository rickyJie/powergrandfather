"""lark_settings: singleton table for outbound Lark push config

Replaces env-var-only config (CSM_LARK_NOTIFY_CHAT_ID / _USER_ID /
_DND_HOURS / _TZ / _DEDUP_SEC). LarkSink now reads this table on every
`send()`; PUT /api/settings/lark updates the row and flushes the sink's
dedup cache. See docs decision folder for context.

Idempotency: seed uses `INSERT OR IGNORE INTO ... WHERE id=1` so a
downgrade + re-upgrade cycle does NOT overwrite manual edits made via
the API. First upgrade reads env vars (backward-compat migration for
existing deployments); subsequent upgrades no-op the seed.

Note on CheckConstraint("id = 1"): Alembic autogenerate can miss this
in future diffs — that's expected. The constraint is enforced at INSERT
time and by the API layer always writing id=1.

Note on env-var handling: `_parse_dnd_hours` swallows malformed tokens
(e.g. "23,foo,1" -> [23, 1]) so a stray env var can't break the
migration. Bad tz strings are stored as-is; LarkSink._load_config
falls back to server-local time on read.

Revision ID: a9u2pd3rfqot
Revises: c4f6h8j0klmn
Create Date: 2026-08-01

Note: this rebases on top of `c4f6h8j0klmn` (in-progress work at the
current chain tip) rather than the earlier `z8t1oc2qdqnr` to preserve a
single head. If `c4f6h8j0klmn` gets reordered before this lands, update
down_revision to whatever revision then sits at the tip.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from datetime import datetime

import sqlalchemy as sa

from alembic import op

revision: str = "a9u2pd3rfqot"
down_revision: str | Sequence[str] | None = "c4f6h8j0klmn"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_log = logging.getLogger("alembic.lark_settings")


def _parse_dnd_hours(raw: str | None) -> list[int]:
    """Tolerant DnD parser. Bad tokens skipped + warned, never raises."""
    if not raw:
        return []
    out: list[int] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            h = int(tok)
        except ValueError:
            _log.warning("lark_settings migration: dropping non-integer dnd token %r", tok)
            continue
        if 0 <= h <= 23:
            out.append(h)
        else:
            _log.warning("lark_settings migration: dropping out-of-range dnd hour %d", h)
    return out


def _parse_dedup(raw: str | None, default: int = 60) -> int:
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        _log.warning("lark_settings migration: bad dedup value %r, using %d", raw, default)
        return default
    return v if v > 0 else default


def upgrade() -> None:
    op.create_table(
        "lark_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("chat_id", sa.String(length=128), nullable=True),
        sa.Column("user_id", sa.String(length=128), nullable=True),
        sa.Column("dedup_window_sec", sa.Integer(), nullable=False, server_default=sa.text("60")),
        sa.Column("dnd_hours", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("tz", sa.String(length=64), nullable=True),
        sa.Column("enabled_types", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_lark_settings_singleton"),
    )

    # Idempotent seed. `INSERT OR IGNORE` is SQLite-specific; if this
    # project ever moves off SQLite, translate to
    # `INSERT ... ON CONFLICT (id) DO NOTHING` (Postgres) or the
    # equivalent MERGE syntax for the target dialect.
    env_chat = os.environ.get("CSM_LARK_NOTIFY_CHAT_ID")
    env_user = os.environ.get("CSM_LARK_NOTIFY_USER_ID")
    env_dnd = os.environ.get("CSM_LARK_DND_HOURS")
    env_tz = os.environ.get("CSM_LARK_TZ")
    env_dedup = os.environ.get("CSM_LARK_DEDUP_SEC")

    # If either target is set in env, treat as opt-in (enabled=True) so
    # existing deployments' behavior doesn't silently regress after upgrade.
    seed_enabled = bool(env_chat or env_user)

    # Explicit list of the 4 legacy PUSH_TYPES defaulted to True. Any
    # NotificationType key missing from this dict is treated as False by
    # the sink (conservative default). Fresh installs still start with
    # enabled=False; the enabled_types payload is only meaningful once
    # the user flips the master switch.
    seed_types = {
        "session_crashed": True,
        "auto_run_failed": True,
        "token_warning": True,
        "port_conflict": True,
    }

    now_iso = datetime.utcnow().isoformat()
    op.execute(
        sa.text(
            """
            INSERT OR IGNORE INTO lark_settings
              (id, enabled, chat_id, user_id, dedup_window_sec,
               dnd_hours, tz, enabled_types, created_at, updated_at)
            VALUES
              (1, :enabled, :chat_id, :user_id, :dedup,
               :dnd, :tz, :types, :now, :now)
            """
        ).bindparams(
            enabled=seed_enabled,
            chat_id=env_chat or None,
            user_id=env_user or None,
            dedup=_parse_dedup(env_dedup),
            dnd=json.dumps(_parse_dnd_hours(env_dnd)),
            tz=env_tz or None,
            types=json.dumps(seed_types),
            now=now_iso,
        )
    )


def downgrade() -> None:
    op.drop_table("lark_settings")
