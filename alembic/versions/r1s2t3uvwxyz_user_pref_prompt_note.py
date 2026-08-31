"""user_preference: default_session_prompt_note + enabled toggle

Follow-up to `p9q0rstuvwxy` (default_session_prompt). The default prompt is
auto-sent as the FIRST user input on INTERACTIVE spawns, and the model
otherwise treats it as a real instruction and burns a turn replying to it.
This adds an optional *supplementary note* that, when enabled, is appended to
the default prompt at delivery time — e.g. "the above is a session-level hint,
no need to reply to this message". Editable text so the user can tune the
wording; toggle so it can be turned on/off independently of the main prompt.

Two new columns on the singleton `user_preference` row:

- `default_session_prompt_note` (TEXT NULL) — the note appended after the
  main prompt. Empty string == "no note" even when the toggle is on.
- `default_session_prompt_note_enabled` (BOOLEAN NOT NULL, DEFAULT 0) —
  master switch. Off by default so upgrades don't change delivered text.

The note is only ever appended when BOTH the main default prompt is being
injected (enabled + non-empty) AND this toggle is on with non-empty text.
Splicing happens in `SessionManager.create_session` at the pref-read site, so
automation / agent-deck / caller-provided prompts are never touched.

Revision ID: r1s2t3uvwxyz
Revises: q0r1stuvwxyz
Create Date: 2026-08-17
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "r1s2t3uvwxyz"
down_revision: str | Sequence[str] | None = "q0r1stuvwxyz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user_preference") as batch:
        batch.add_column(
            sa.Column("default_session_prompt_note", sa.Text(), nullable=True),
        )
        batch.add_column(
            sa.Column(
                "default_session_prompt_note_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("user_preference") as batch:
        batch.drop_column("default_session_prompt_note_enabled")
        batch.drop_column("default_session_prompt_note")
