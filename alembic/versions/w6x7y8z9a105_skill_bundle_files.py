"""skill bundle sync: skill_file table + skill.last_synced_files

Before this, skill sync only ever materialised `<skills_dir>/<name>/SKILL.md`
on the target agent. Any skill shipping sibling files — `query.py`,
`references/*.md`, `scripts/*.py` — arrived structurally incomplete, and the
drift poller never noticed because it only checked that the *directory*
existed.

This adds:
- `skill_file`: one row per non-SKILL.md file in the bundle, holding the raw
  bytes plus the permission bits (the exec bit matters — a non-executable
  `query.py` is as broken as a missing one).
- `skill.last_synced_files`: `{agent: {rel_path: sha256}}`, the manifest last
  written per agent. Prune on the next push is scoped to these paths so a
  hand-placed file in the target dir is never collateral damage.

Backfill is deliberately NOT done here: the bundle has to be read off the
source agent's disk, which is the adapter layer's job, not a migration's.
Use `POST /api/sync/skills/reingest` after upgrading.

Revision ID: w6x7y8z9a105
Revises: v5w6x7y8z904
Create Date: 2026-08-30
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "w6x7y8z9a105"
down_revision: str | Sequence[str] | None = "v5w6x7y8z904"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "skill_file",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("skill_id", sa.Integer(), nullable=False),
        sa.Column("rel_path", sa.String(length=512), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("mode", sa.Integer(), server_default="420", nullable=False),  # 0o644
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["skill_id"], ["skill.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("skill_id", "rel_path", name="uq_skill_file_path"),
    )
    op.create_index("ix_skill_file_skill_id", "skill_file", ["skill_id"])

    with op.batch_alter_table("skill") as batch:
        batch.add_column(
            sa.Column("last_synced_files", sa.JSON(), server_default="{}", nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("skill") as batch:
        batch.drop_column("last_synced_files")

    op.drop_index("ix_skill_file_skill_id", table_name="skill_file")
    op.drop_table("skill_file")
