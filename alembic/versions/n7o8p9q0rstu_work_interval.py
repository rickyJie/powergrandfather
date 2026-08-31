"""worktime: work_interval table

Adds `work_interval` — an append-only log of human / agent activity intervals
that drives the header top-right worktime widget.

Two `kind` values are recognized:

- `human`  — a UI-focused interval on the CSM frontend. Opened by the first
  heartbeat, extended by subsequent heartbeats within a 60s grace window,
  closed when the grace lapses or on server-boot reap. `session_id` NULL.
- `agent`  — a per-Claude-session compute interval, opened on
  `message.user_sent` and closed on `message.assistant_done` /
  terminal-session events / a 30-min safety cap.

Wall-clock accumulation (no union-collapse): overlapping intervals across
sessions each count fully.

Revision ID: n7o8p9q0rstu
Revises: m5n6o7p8q9rs
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "n7o8p9q0rstu"
down_revision: str | Sequence[str] | None = "m5n6o7p8q9rs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "work_interval",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("start_ts", sa.DateTime(), nullable=False),
        sa.Column("end_ts", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(16), nullable=False, server_default="event"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_wi_kind_start", "work_interval", ["kind", "start_ts"])
    op.create_index("ix_wi_session_start", "work_interval", ["session_id", "start_ts"])
    op.create_index("ix_wi_open", "work_interval", ["end_ts"])


def downgrade() -> None:
    op.drop_index("ix_wi_open", table_name="work_interval")
    op.drop_index("ix_wi_session_start", table_name="work_interval")
    op.drop_index("ix_wi_kind_start", table_name="work_interval")
    op.drop_table("work_interval")
