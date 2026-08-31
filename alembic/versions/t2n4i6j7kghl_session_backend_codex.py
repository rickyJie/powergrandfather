"""session.backend + session.codex_rollout_path (codex multi-CLI support)

Adds two columns to `session`:

- `backend` — string enum {'claude', 'codex'}, NOT NULL, default 'claude'.
  Discriminator for which CLI a session was spawned against. All existing
  rows are Claude, so backfill = 'claude'.
- `codex_rollout_path` — nullable string. Absolute path to the codex
  `rollout-*.jsonl` for this session once the tailer has matched it
  (post-hoc binding — codex has no `--session-id`).

Revision ID: t2n4i6j7kghl
Revises: s1m3h5i6jfgk
Create Date: 2026-07-25
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "t2n4i6j7kghl"
down_revision: str | Sequence[str] | None = "s1m3h5i6jfgk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SQLite: server_default 'claude' backfills existing rows on ALTER TABLE.
    op.add_column(
        "session",
        sa.Column(
            "backend",
            sa.String(length=16),
            nullable=False,
            server_default="claude",
        ),
    )
    op.add_column(
        "session",
        sa.Column(
            "codex_rollout_path",
            sa.String(length=2048),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_session_backend",
        "session",
        ["backend"],
    )


def downgrade() -> None:
    # NOTE: `DROP COLUMN` is only supported on SQLite >= 3.35 (2021-03-12).
    # Older sqlite (e.g. RHEL7 system Python bundled 3.7.x) will raise
    # `OperationalError: near "DROP": syntax error`. Every conda csm env
    # we ship pins Python 3.11, which bundles sqlite >= 3.40, so this is
    # only a concern for exotic/system-python downgrades.
    op.drop_index("ix_session_backend", table_name="session")
    op.drop_column("session", "codex_rollout_path")
    op.drop_column("session", "backend")
