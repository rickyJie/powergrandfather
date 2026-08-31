"""repair orphan rows and terminal mission run state

Revision ID: a9u2pd3rer0s
Revises: z8t1oc2qdqnr
Create Date: 2026-07-30
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a9u2pd3rer0s"
down_revision: str | Sequence[str] | None = "z8t1oc2qdqnr"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Before multi-agent attribution existed, every token event/rollup was
    # produced by the only supported adapter (Claude). Leaving these rows
    # NULL makes the new default `agent=claude` Tokens view look empty.
    op.execute("UPDATE raw_token_event SET agent = 'claude' WHERE agent IS NULL OR agent = ''")
    op.execute("UPDATE hourly_rollup SET agent = 'claude' WHERE agent IS NULL OR agent = ''")
    with op.batch_alter_table("raw_token_event") as batch:
        batch.alter_column(
            "agent",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="claude",
        )
    with op.batch_alter_table("hourly_rollup") as batch:
        batch.alter_column(
            "agent",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="claude",
        )
    # Agent is part of rollup identity. Without it, Claude and Codex using
    # the same model/project/hour overwrite each other during upsert.
    op.drop_index("ix_hr_bucket_model_proj", table_name="hourly_rollup")
    op.create_index(
        "ix_hr_bucket_model_proj_agent",
        "hourly_rollup",
        ["bucket_hour", "model", "project_path", "agent"],
        unique=True,
    )

    # Deleted workflow definitions intentionally retain mission history.
    # Represent that state as NULL rather than an invalid foreign-key value.
    with op.batch_alter_table("mission") as batch:
        batch.alter_column(
            "workflow_def_id",
            existing_type=sa.String(length=36),
            nullable=True,
        )
    op.execute(
        "UPDATE mission SET workflow_def_id = NULL "
        "WHERE NOT EXISTS (SELECT 1 FROM workflow_definition "
        "WHERE workflow_definition.id = mission.workflow_def_id)"
    )

    # SQLite foreign keys were historically disabled on application
    # connections, so cascades declared by the ORM did not run.
    op.execute(
        "DELETE FROM session_file_touch "
        "WHERE NOT EXISTS (SELECT 1 FROM session WHERE session.id = session_file_touch.sid)"
    )
    op.execute(
        "DELETE FROM agent_conversation "
        "WHERE NOT EXISTS (SELECT 1 FROM session WHERE session.id = agent_conversation.session_id)"
    )
    op.execute(
        "DELETE FROM notification WHERE session_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM session WHERE session.id = notification.session_id)"
    )

    # A terminal mission must not retain a RUNNING/PENDING stage row. These
    # rows predate the terminal-stamping path and are otherwise invisible to
    # the rescuer, which intentionally scans RUNNING missions only.
    op.execute(
        "UPDATE stage_execution SET status = 'FAILED', ended_at = CURRENT_TIMESTAMP "
        "WHERE status IN ('RUNNING', 'PENDING') AND mission_id IN "
        "(SELECT id FROM mission WHERE status IN ('FAILED', 'CANCELLED'))"
    )
    op.execute(
        "UPDATE stage_execution SET status = 'SUCCEEDED', ended_at = CURRENT_TIMESTAMP "
        "WHERE status IN ('RUNNING', 'PENDING') AND mission_id IN "
        "(SELECT id FROM mission WHERE status = 'SUCCEEDED')"
    )


def downgrade() -> None:
    op.drop_index("ix_hr_bucket_model_proj_agent", table_name="hourly_rollup")
    op.create_index(
        "ix_hr_bucket_model_proj",
        "hourly_rollup",
        ["bucket_hour", "model", "project_path"],
        unique=True,
    )
