"""session.pinned + session.manual_unread

Right-click context-menu additions (local:45b259b4):
- `pinned`: sort a session to the top of its folder (or the whole tree
  when there's no folder grouping).
- `manual_unread`: sticky flag that keeps the red unread badge visible
  even when unread_count is 0 — an escape hatch for "come back to this".

Both default to false; every existing row backfills to false via the
server_default clause on ADD COLUMN.

Revision ID: r0l2g4h5iefj
Revises: q9k1f3g4hdei
Create Date: 2026-07-24
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "r0l2g4h5iefj"
down_revision: str | Sequence[str] | None = "5386586e6240"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.add_column(
            sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )
        batch.add_column(
            sa.Column("manual_unread", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        )


def downgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.drop_column("manual_unread")
        batch.drop_column("pinned")
