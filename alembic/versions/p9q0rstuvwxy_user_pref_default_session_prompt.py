"""user_preference: default_session_prompt + enabled toggle

Follow-up for the LaTeX rendering discussion — cheapest defensible fix
is to let the user configure a system-preamble text that CSM
auto-injects as the FIRST user input to any INTERACTIVE session on
spawn. AUTO / CHAT_AGENT sessions skip this so workflow-defined
prompts / agent-definition prompts aren't polluted by a global default.

Two new columns on the singleton `user_preference` row:

- `default_session_prompt` (TEXT NULL) — the text to inject. Empty
  string == "no override" even when the toggle is on.
- `default_session_prompt_enabled` (BOOLEAN NOT NULL, DEFAULT 0) —
  master switch. Off by default so upgrades don't silently start
  sending a first prompt.

Delivery path is the pre-existing
`SessionManager._deliver_initial_prompt` — 3s post-spawn REPL warm-up
+ write bytes + CRLF, guarded by the write_lock. No CLI-specific
mechanism (this is deliberately not `--append-system-prompt` because
codex has no clean equivalent; PTY input works uniformly).

Revision ID: p9q0rstuvwxy
Revises: o8p9q0rstuvw
Create Date: 2026-08-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "p9q0rstuvwxy"
down_revision: str | Sequence[str] | None = "o8p9q0rstuvw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_preference") as batch:
        batch.add_column(
            sa.Column("default_session_prompt", sa.Text(), nullable=True),
        )
        batch.add_column(
            sa.Column(
                "default_session_prompt_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("user_preference") as batch:
        batch.drop_column("default_session_prompt_enabled")
        batch.drop_column("default_session_prompt")
