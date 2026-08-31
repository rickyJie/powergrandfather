"""add session_project table + session.session_project_id

Revision ID: 5386586e6240
Revises: 1ceb24911705
Create Date: 2026-07-14 20:33:44.280370

local:a79c795d — user-managed project grouping for interactive sessions.
Independent of workflow.Project (separate `project` table). NULL FK means
the session falls back to the auto-derived "cwd 2-level" virtual group.

Autogenerate produced a lot of noise from pre-existing schema drift
(budget/feedback/mission enum types, stage_execution index renames,
workflow_definition review_status, etc.) — pruned out here so this
migration only touches what the feature actually needs. Drift cleanup
should live in a dedicated migration, not ride along with unrelated
schema changes.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '5386586e6240'
down_revision: str | Sequence[str] | None = '1ceb24911705'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'session_project',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('archived_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('session_project', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_session_project_archived_at'), ['archived_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_session_project_name'), ['name'], unique=True)

    with op.batch_alter_table('session', schema=None) as batch_op:
        batch_op.add_column(sa.Column('session_project_id', sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f('ix_session_session_project_id'), ['session_project_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('session', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_session_session_project_id'))
        batch_op.drop_column('session_project_id')

    with op.batch_alter_table('session_project', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_session_project_name'))
        batch_op.drop_index(batch_op.f('ix_session_project_archived_at'))
    op.drop_table('session_project')
