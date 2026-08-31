"""session: backfill title_manual for existing non-empty titles

The `title_manual` flag was added in `o8p9q0rstuvw` to protect UI-renamed
titles from being overwritten by claude ai-title / custom-title sync. At
that time only PATCH `/api/sessions/{sid}` set the flag; create-time
titles (typed into the CSM new-session form) landed with
`title_manual=0` even though semantically they represent the same user
claim on the field.

Follow-up (this migration): retroactively mark any existing row with a
non-empty title as `title_manual=1`. A user who wants a fresh ai-title
can PATCH title="" to release the claim.

Revision ID: q0r1stuvwxyz
Revises: p9q0rstuvwxy
Create Date: 2026-08-17
"""
from collections.abc import Sequence

from alembic import op

revision: str = "q0r1stuvwxyz"
down_revision: str | Sequence[str] | None = "p9q0rstuvwxy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE session SET title_manual = 1 "
        "WHERE title IS NOT NULL AND TRIM(title) != '' AND title_manual = 0"
    )


def downgrade() -> None:
    # One-way backfill — the flag now carries the true user-intent state,
    # so reverting would fabricate "not manual" for rows that are.
    pass
