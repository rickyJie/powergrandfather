"""SyncActivity ORM model — append-only per-CRUD sync attempt log.

One row per (resource, agent, action) sync attempt. `detail_json` holds
the redacted argv, stderr, and any adapter-specific diagnostic — NEVER
raw env values (spec §5 B5). Retention is applied by RollupWorker at
the same tick (spec Non-blocking N2, default 30d).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class SyncActivity(Base):
    __tablename__ = "sync_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, nullable=False)

    # SyncModule enum value.
    module: Mapped[str] = mapped_column(String(32), nullable=False)

    # DriftResourceType enum value.
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # PK in the corresponding resource table. Nullable for full-module
    # rebuilds ("reconcile everything for this module on this agent").
    resource_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    agent: Mapped[str] = mapped_column(String(32), nullable=False)

    # "add" | "remove" | "update" | "probe"
    action: Mapped[str] = mapped_column(String(16), nullable=False)

    # SyncStatus enum value.
    status: Mapped[str] = mapped_column(String(16), nullable=False)

    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    detail_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
