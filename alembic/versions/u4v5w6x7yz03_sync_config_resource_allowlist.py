"""sync_config: resource_allowlist (per-module resource-name selection)

Adds an optional allowlist of resource NAMES a sync module may sync. `NULL`
(default) = no filter, consider everything the agent exposes (backward
compatible). A JSON list restricts sync to exactly those names.

Primary use: the `skills` module, so the user syncs only the skills they picked
rather than every skill installed under ~/.claude/skills/ (marketplace-installed
and hand-authored skills are mixed there with no reliable source marker, so
selection has to be explicit). Matched by name — no hard-coded prefixes/counts,
generalizes to any environment.

Column:
- `resource_allowlist` (JSON, NULL) — NULL = all; list = only those names.

Revision ID: u4v5w6x7yz03
Revises: t3u4v5w6xy02
Create Date: 2026-08-23
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "u4v5w6x7yz03"
down_revision: str | Sequence[str] | None = "t3u4v5w6xy02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("sync_config") as batch:
        batch.add_column(
            sa.Column("resource_allowlist", sa.JSON(), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("sync_config") as batch:
        batch.drop_column("resource_allowlist")
