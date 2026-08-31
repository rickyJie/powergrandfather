"""multi-agent sync P0: 6 tables (sync_config / instruction / mcp_server / skill / sync_activity / drift_record).

Introduces the persistence layer for the CSM multi-agent config sync
subsystem. See `docs/backends/multi_agent_sync_spec.md` for the full DDL
and B1-B5 decisions this schema is shaped around.

Key design notes baked into the schema:
- `sync_config.module` has a UNIQUE constraint (spec §4 B4-N6): one row
  per SyncModule ("memory" | "mcp" | "skills"), no accidental doubles.
- `drift_record.(resource_type, resource_id)` is the cross-table pointer
  used in place of a polymorphic FK. Enforced in Python; SQLite has no
  DB-level guarantee, but retaining drift log rows after a resource is
  hard-deleted is a feature (audit).
- `sync_activity.resource_id` is nullable because a full-module rebuild
  is a legitimate row shape (no single-resource target).
- Every JSON list / dict column is TEXT (SQLite has no native JSONB).

Revision ID: y7s0nb1pcpmq
Revises: x6r8ma0nbokp
Create Date: 2026-07-29
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "y7s0nb1pcpmq"
down_revision: str | Sequence[str] | None = "x6r8ma0nbokp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---- 1. sync_config -----------------------------------------
    op.create_table(
        "sync_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("module", sa.String(length=32), nullable=False, unique=True),
        sa.Column("enrolled_agents", sa.JSON, nullable=False),
        sa.Column("poll_interval_sec", sa.Integer, nullable=False,
                  server_default=sa.text("30")),
        sa.Column("enabled", sa.Boolean, nullable=False,
                  server_default=sa.text("1")),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    # ---- 2. instruction -----------------------------------------
    op.create_table(
        "instruction",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("share_scope", sa.JSON, nullable=False),
        sa.Column("priority", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_instruction_name", "instruction", ["name"])

    # ---- 3. mcp_server ------------------------------------------
    op.create_table(
        "mcp_server",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False, unique=True),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("command", sa.Text, nullable=True),
        sa.Column("args_json", sa.JSON, nullable=False),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("env_json", sa.JSON, nullable=False),
        sa.Column("enabled_for", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_mcp_server_name", "mcp_server", ["name"])

    # ---- 4. skill -----------------------------------------------
    op.create_table(
        "skill",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=64), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=False),
        sa.Column("body_md", sa.Text, nullable=False),
        sa.Column("share_scope", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_skill_name", "skill", ["name"])

    # ---- 5. sync_activity ---------------------------------------
    op.create_table(
        "sync_activity",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime, nullable=False),
        sa.Column("module", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.Integer, nullable=True),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("detail_json", sa.JSON, nullable=True),
    )
    op.create_index(
        "ix_sync_activity_module_ts",
        "sync_activity",
        ["module", sa.text("ts DESC")],
    )
    op.create_index(
        "ix_sync_activity_resource",
        "sync_activity",
        ["resource_type", "resource_id"],
    )

    # ---- 6. drift_record ----------------------------------------
    op.create_table(
        "drift_record",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime, nullable=False),
        sa.Column("module", sa.String(length=32), nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("resource_id", sa.Integer, nullable=False),
        sa.Column("agent", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=32), nullable=False),
        sa.Column("expected_hash", sa.Text, nullable=True),
        sa.Column("actual_hash", sa.Text, nullable=True),
        sa.Column("resolved", sa.Boolean, nullable=False,
                  server_default=sa.text("0")),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("detail_json", sa.JSON, nullable=True),
    )
    # Partial index — only unresolved rows are hot; keeps the index small.
    op.create_index(
        "ix_drift_record_unresolved",
        "drift_record",
        ["resolved", sa.text("ts DESC")],
        sqlite_where=sa.text("resolved = 0"),
    )
    op.create_index(
        "ix_drift_record_resource",
        "drift_record",
        ["resource_type", "resource_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_drift_record_resource", table_name="drift_record")
    op.drop_index("ix_drift_record_unresolved", table_name="drift_record")
    op.drop_table("drift_record")

    op.drop_index("ix_sync_activity_resource", table_name="sync_activity")
    op.drop_index("ix_sync_activity_module_ts", table_name="sync_activity")
    op.drop_table("sync_activity")

    op.drop_index("ix_skill_name", table_name="skill")
    op.drop_table("skill")

    op.drop_index("ix_mcp_server_name", table_name="mcp_server")
    op.drop_table("mcp_server")

    op.drop_index("ix_instruction_name", table_name="instruction")
    op.drop_table("instruction")

    op.drop_table("sync_config")
