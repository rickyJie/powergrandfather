"""session.highlighted

Context-menu addition (local:45b259b4 follow-up):
- `highlighted`: user-marked "important" flag rendered as a gold accent
  on the tree row. Orthogonal to `pinned` (which reorders) and
  `manual_unread` (which is a red badge), so users can combine.

Backfills to false via server_default on ADD COLUMN.

Revision ID: z8t1oc2qdqnr
Revises: y7s0nb1pcpmq
Create Date: 2026-07-30

Rebased for llj_dev_codex merge: originally down_revision=s1m3h5i6jfgk
(the pre-multi-agent state on llj_dev), moved to y7s0nb1pcpmq (the tail
of the codex-branch multi-agent chain) so both branches' migrations
apply cleanly on the merged tree.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "z8t1oc2qdqnr"
down_revision: str | Sequence[str] | None = "y7s0nb1pcpmq"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.add_column(
            sa.Column(
                "highlighted",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.drop_column("highlighted")
