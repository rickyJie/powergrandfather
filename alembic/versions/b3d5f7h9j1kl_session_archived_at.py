"""session.archived_at

Soft archive separates routine history cleanup from permanent deletion.

Revision ID: b3d5f7h9j1kl
Revises: a9u2pd3rer0s
Create Date: 2026-07-31
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b3d5f7h9j1kl"
down_revision: str | Sequence[str] | None = "a9u2pd3rer0s"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.create_index("ix_session_archived_at", ["archived_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.drop_index("ix_session_archived_at")
        batch.drop_column("archived_at")
