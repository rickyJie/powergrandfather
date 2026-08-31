"""ScheduleEntry ORM model — cron schedule bound to a WorkflowDefinition.

Post-P2 (workflow-only): every schedule row points at a WorkflowDefinition
via `workflow_def_id`. M4 TaskDefinition is retired; `task_def_id` was
dropped from this table in migration g9b2c3d4e5f6.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class ScheduleEntry(Base):
    """A scheduled launch of a WorkflowDefinition.

    Timing form: exactly one of `cron` (recurring) or `run_at` (one-shot,
    auto-disables after firing).
    """

    __tablename__ = "schedule_entry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_def_id: Mapped[str] = mapped_column(String(36), index=True)
    cron: Mapped[str | None] = mapped_column(String(100), nullable=True)
    run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
