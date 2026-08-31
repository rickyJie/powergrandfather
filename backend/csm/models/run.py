"""Stage-execution ORM model.

P3 of the workflow/task consolidation: the M4 concept of a "Run" (a
single execution of a TaskDefinition) is retired. This model now
represents **one execution of one stage inside a workflow mission**.

Naming:
- Python class stays `Run` (touched in 15+ files across supervisor,
  aggregator, poll_executor, session_manager — a full rename is a
  separate refactor). An alias `StageExecution = Run` is exported for
  callers who prefer the new name.
- DB table renamed `run` → `stage_execution` in migration h0c3d4e5f6a7.
- `task_def_id` column dropped in the same migration.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


# Post-P3 alias — new code should prefer StageExecutionStatus for clarity.
StageExecutionStatus = RunStatus


class Run(Base):
    """One execution of one stage inside a workflow mission.

    (Legacy name kept for backwards-compat with in-flight callers; new
    code should reference the `StageExecution` alias below.)
    """

    __tablename__ = "stage_execution"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    schedule_entry_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    status: Mapped[RunStatus] = mapped_column(SAEnum(RunStatus, native_enum=False), default=RunStatus.PENDING)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mission_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    stage_name: Mapped[str | None] = mapped_column(String(200), nullable=True)


# Public alias: use StageExecution in new code.
StageExecution = Run
