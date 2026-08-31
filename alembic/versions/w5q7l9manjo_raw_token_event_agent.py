"""raw_token_event.agent + hourly_rollup.agent for agent-scoped Tokens view.

M12. Adds an `agent` column to `raw_token_event` (indexed) so the Tokens
page can filter to a single CLI-adapter's rows — user requirement:
"default view = my default agent, with a toggle to switch". Previously
the Tokens page mixed claude + codex data with no way to slice.

Also adds `agent` to `hourly_rollup` (indexed) so cross-window queries
(anything > raw_token_event TTL) can honor the same filter.

Existing rows get NULL — they're unclassified data from before this
change. Frontend renders NULL rows under "unclassified" so users can
still see them; new rows will always carry `agent`.

Revision ID: w5q7l9manjo
Revises: v4p6k8l9mijn
Create Date: 2026-07-26
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "w5q7l9manjo"
down_revision: str | Sequence[str] | None = "v4p6k8l9mijn"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "raw_token_event",
        sa.Column("agent", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_rte_agent", "raw_token_event", ["agent"])

    op.add_column(
        "hourly_rollup",
        sa.Column("agent", sa.String(length=32), nullable=True),
    )
    op.create_index("ix_hr_agent", "hourly_rollup", ["agent"])


def downgrade() -> None:
    op.drop_index("ix_hr_agent", table_name="hourly_rollup")
    op.drop_column("hourly_rollup", "agent")
    op.drop_index("ix_rte_agent", table_name="raw_token_event")
    op.drop_column("raw_token_event", "agent")
