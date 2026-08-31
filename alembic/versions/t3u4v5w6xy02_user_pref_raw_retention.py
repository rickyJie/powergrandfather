"""user_preference: raw_event_retention_days (runtime-managed TTL window)

Makes the `raw_token_event` retention window runtime-editable. Previously the
window lived only in `settings.raw_event_retention_days` (env
`CSM_RAW_EVENT_RETENTION_DAYS`, default 0 = keep forever) and could only be
changed with a restart. This adds a persisted column on the singleton
`user_preference` row that the RollupWorker reads fresh on every hourly tick,
so PUT /api/preferences can retune it live.

Column:
- `raw_event_retention_days` (INTEGER NOT NULL, DEFAULT 180) —
    0 → keep raw events forever; N → delete raw older than N days (after they've
    been rolled up into hourly_rollup, so trend charts keep full history).

Default 180 (≈ half a year) is chosen so per-session/task drill-down stays
available for a long time and monthly budgets (which read RAW for the whole
current calendar month) are always safely covered — the practical floor for a
non-zero value is ~35 days.

Existing installs: the singleton row gets 180 via server_default, i.e. the
formerly-unbounded raw table now caps at ~180 days going forward. Nothing is
deleted by this migration itself; the RollupWorker performs the (rollup-then-)
delete on its next tick, and only for rows genuinely older than the window.

Revision ID: t3u4v5w6xy02
Revises: s2t3u4wxyz01
Create Date: 2026-08-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "t3u4v5w6xy02"
down_revision: str | Sequence[str] | None = "s2t3u4wxyz01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_preference") as batch:
        batch.add_column(
            sa.Column(
                "raw_event_retention_days",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("180"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("user_preference") as batch:
        batch.drop_column("raw_event_retention_days")
