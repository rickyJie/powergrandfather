"""usage_snapshot: add agent discriminator + codex-specific fields.

M14. codex has its own `/usage` panel with a completely different shape
(Lifetime / Peak / Streak / Longest task — total usage, no reset%).
Rather than a parallel table, extend `usage_snapshot` with:

- `agent VARCHAR(32) NOT NULL DEFAULT 'claude'` — discriminator; every
  existing row is a claude probe, backfill accordingly
- `ix_us_agent_ts` index for "latest per agent" queries
- codex fields: lifetime / peak / streak / longest_task, all optional
  since only codex probes populate them

Frontend reads `/api/tokens/usage-live?agent=<name>` to fetch the latest
snapshot per adapter.

Revision ID: x6r8ma0nbokp
Revises: w5q7l9manjo
Create Date: 2026-07-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "x6r8ma0nbokp"
down_revision: str | Sequence[str] | None = "w5q7l9manjo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Discriminator + composite index for "latest per agent" reads.
    op.add_column(
        "usage_snapshot",
        sa.Column(
            "agent", sa.String(length=32), nullable=False,
            server_default="claude",
        ),
    )
    op.create_index("ix_us_agent_ts", "usage_snapshot", ["agent", "ts"])

    # Codex-specific fields. All nullable — a claude probe leaves them all None.
    op.add_column(
        "usage_snapshot",
        sa.Column("codex_lifetime_tokens", sa.BigInteger, nullable=True),
    )
    op.add_column(
        "usage_snapshot",
        sa.Column("codex_peak_daily_tokens", sa.BigInteger, nullable=True),
    )
    op.add_column(
        "usage_snapshot",
        sa.Column("codex_streak_days", sa.Integer, nullable=True),
    )
    op.add_column(
        "usage_snapshot",
        sa.Column("codex_longest_task_sec", sa.Integer, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("usage_snapshot", "codex_longest_task_sec")
    op.drop_column("usage_snapshot", "codex_streak_days")
    op.drop_column("usage_snapshot", "codex_peak_daily_tokens")
    op.drop_column("usage_snapshot", "codex_lifetime_tokens")
    op.drop_index("ix_us_agent_ts", table_name="usage_snapshot")
    op.drop_column("usage_snapshot", "agent")
