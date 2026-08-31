"""session_file_touch table

Tracks every Write/Edit/MultiEdit/Create tool invocation observed via
the PreToolUse hook. Powers the "📄 Files (N)" popover in the session
header — user clicks to preview any file claude has touched in this
session without hunting through the tree.

Rows cascade-delete with the parent session (FK ON DELETE CASCADE);
the hook handler prunes to a per-session cap of 100.

Revision ID: s1m3h5i6jfgk
Revises: r0l2g4h5iefj
Create Date: 2026-07-24
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "s1m3h5i6jfgk"
down_revision: str | Sequence[str] | None = "r0l2g4h5iefj"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "session_file_touch",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "sid",
            sa.String(length=36),
            sa.ForeignKey("session.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.String(length=2048), nullable=False),
        sa.Column("tool", sa.String(length=32), nullable=False),
        sa.Column("ts", sa.DateTime(), nullable=False, server_default=sa.func.current_timestamp()),
    )
    op.create_index(
        "ix_session_file_touch_sid",
        "session_file_touch",
        ["sid"],
    )
    op.create_index(
        "ix_session_file_touch_sid_ts",
        "session_file_touch",
        ["sid", "ts"],
    )


def downgrade() -> None:
    op.drop_index("ix_session_file_touch_sid_ts", table_name="session_file_touch")
    op.drop_index("ix_session_file_touch_sid", table_name="session_file_touch")
    op.drop_table("session_file_touch")
