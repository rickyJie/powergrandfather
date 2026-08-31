"""Multi-agent v2 architecture: user_preference + session.agent + file_state.agent

This migration completes the multi-CLI adapter refactor by taking the
schema from "codex is a bolted-on second backend" to "N-agent adapter
layer with user preference as the resolution root".

# Changes

## New table `user_preference` (single row)

Persists the user's default agent choice + first-run wizard flag +
optional supervisor-agent override. Single-user local app, so exactly
one row (id=1, enforced by CHECK constraint).

Seeded on upgrade with `default_agent='claude'` and
`has_completed_first_run=1` (existing installs pre-date the wizard, so
we assume they've "seen it"; a fresh DB will have the row seeded with
`has_completed_first_run=0`, giving new users the wizard flow).

## Rename `session.backend` → `session.agent`

Renames the discriminator column so its name matches the new
architecture vocabulary (adapter = "agent" throughout the frontend and
API). Column type stays VARCHAR(16) — the M2 (`t2n4i6j7kghl`) migration
already used a string column with `native_enum=False`, so no data
conversion needed. The `SessionBackend` enum will be dropped from the
ORM in the model change accompanying this migration; existing rows just
carry through as 'claude' / 'codex' text.

## Rename `session.codex_rollout_path` → `session.rollout_path`

Generalises the column since it's no longer codex-specific: any adapter
that discovers a per-session artifact path post-spawn writes here.

## `file_state`: add `agent` column, rename `jsonl_path` → `artifact_path`

Adds a required `agent` column (default 'claude' for backfill) so
per-adapter tail state is namespaced. Renames `jsonl_path` →
`artifact_path` since codex writes to `rollout-*.jsonl`, not "jsonl in
general" — the old name was misleading.

The primary key stays single-column (artifact_path) because claude and
codex artifact roots never overlap (claude: `~/.claude/projects/`,
codex: `~/.codex/sessions/`) — path is unique globally. A composite
`(artifact_path, agent)` PK was considered but rejected as needless
complexity for a non-existent conflict.

Adds `ix_file_state_agent` for potential filter-by-agent queries.

## Drops the old `ix_session_backend` index, adds `ix_session_agent`

Index rename to match the column rename.

# Downgrade path

SQLite 3.25+ supports `ALTER TABLE ... RENAME COLUMN` natively. All
CSM conda envs pin Python 3.11 which bundles SQLite ≥ 3.40, so this is
safe. `DROP COLUMN` requires 3.35+ (same story). The downgrade fully
reverses the schema — new columns dropped, old names restored, the
`user_preference` table torn down.

Revision ID: u3o5j7k8lhim
Revises: t2n4i6j7kghl
Create Date: 2026-07-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "u3o5j7k8lhim"
down_revision: str | Sequence[str] | None = "t2n4i6j7kghl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. New user_preference table (single row).
    # ------------------------------------------------------------------
    op.create_table(
        "user_preference",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "default_agent",
            sa.String(length=32),
            nullable=False,
            server_default="claude",
        ),
        sa.Column(
            "supervisor_agent",
            sa.String(length=32),
            nullable=True,
        ),
        sa.Column(
            "has_completed_first_run",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.CheckConstraint("id = 1", name="ck_user_preference_singleton"),
    )
    # Seed the single row. Existing installs pre-date the wizard —
    # has_completed_first_run defaults to 1 so upgrades don't re-prompt.
    op.execute(
        "INSERT INTO user_preference (id, default_agent, has_completed_first_run) "
        "VALUES (1, 'claude', 1)"
    )

    # ------------------------------------------------------------------
    # 2. session.backend → session.agent.
    # ------------------------------------------------------------------
    op.drop_index("ix_session_backend", table_name="session")
    op.alter_column("session", "backend", new_column_name="agent")
    op.create_index("ix_session_agent", "session", ["agent"])

    # ------------------------------------------------------------------
    # 3. session.codex_rollout_path → session.rollout_path.
    # ------------------------------------------------------------------
    op.alter_column(
        "session",
        "codex_rollout_path",
        new_column_name="rollout_path",
    )

    # ------------------------------------------------------------------
    # 4. file_state: add agent column, rename jsonl_path → artifact_path.
    # ------------------------------------------------------------------
    op.add_column(
        "file_state",
        sa.Column(
            "agent",
            sa.String(length=32),
            nullable=False,
            server_default="claude",
        ),
    )
    op.alter_column(
        "file_state",
        "jsonl_path",
        new_column_name="artifact_path",
    )
    op.create_index("ix_file_state_agent", "file_state", ["agent"])


def downgrade() -> None:
    # Reverse in strict inverse order.

    # 4. file_state.
    op.drop_index("ix_file_state_agent", table_name="file_state")
    op.alter_column(
        "file_state",
        "artifact_path",
        new_column_name="jsonl_path",
    )
    op.drop_column("file_state", "agent")

    # 3. session.rollout_path → session.codex_rollout_path.
    op.alter_column(
        "session",
        "rollout_path",
        new_column_name="codex_rollout_path",
    )

    # 2. session.agent → session.backend.
    op.drop_index("ix_session_agent", table_name="session")
    op.alter_column("session", "agent", new_column_name="backend")
    op.create_index("ix_session_backend", "session", ["backend"])

    # 1. Drop user_preference table.
    op.drop_table("user_preference")
