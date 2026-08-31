"""sync_config: tick_interval_minutes (finer-grained agent-tick cadence)

Follow-up to `m5n6o7p8q9rs` (sync v2 agent-driven). The v2 SyncTickScheduler
only supported `tick_interval_hours` (integer hours), so an agent-mode module
could tick at most hourly. This adds `tick_interval_minutes` for finer
cadence: when >0 it takes precedence over `tick_interval_hours` (minutes wins
over hours); 0 falls back to hours. The scheduler clamps effective cadence to
>= 1 minute so a stray small value can't busy-spin the 60s loop.

One new column on `sync_config`:

- `tick_interval_minutes` (INTEGER NOT NULL, DEFAULT 0) — 0 = fall back to
  tick_interval_hours; N>0 = scheduled every N minutes (agent mode only).

Revision ID: s2t3u4wxyz01
Revises: r1s2t3uvwxyz
Create Date: 2026-08-17
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "s2t3u4wxyz01"
down_revision: str | Sequence[str] | None = "r1s2t3uvwxyz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_config") as batch:
        batch.add_column(
            sa.Column(
                "tick_interval_minutes",
                sa.Integer(),
                nullable=True,
                server_default="0",
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_config") as batch:
        batch.drop_column("tick_interval_minutes")
