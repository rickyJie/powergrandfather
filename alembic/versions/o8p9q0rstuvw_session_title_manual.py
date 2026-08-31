"""session.title_manual

`local:7a422f9d` — external title sync (claude custom-title records,
codex threads.title) must NOT clobber a title the user typed via CSM
UI. Adapters now guard on `title_manual`:

- `false` (default) → adapters are free to overwrite `title` from the
  external source (claude custom-title / ai-title record, or codex
  threads.title column).
- `true` → CSM UI is authoritative. Adapters skip the update. Reset
  to `false` by clearing the title to null in the UI.

Backfills to false via server_default on ADD COLUMN.

Revision ID: o8p9q0rstuvw
Revises: n7o8p9q0rstu
Create Date: 2026-08-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "o8p9q0rstuvw"
down_revision: str | Sequence[str] | None = "n7o8p9q0rstu"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.add_column(
            sa.Column(
                "title_manual",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("session") as batch:
        batch.drop_column("title_manual")
