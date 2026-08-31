"""Mission ORM model — one instance of a WorkflowDefinition with launch parameters."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class MissionStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Mission(Base):
    __tablename__ = "mission"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workflow_def_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("workflow_definition.id"),
        nullable=True,
        index=True,
    )
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    workspace_path: Mapped[str] = mapped_column(String(1024))
    status: Mapped[MissionStatus] = mapped_column(
        SAEnum(MissionStatus, native_enum=False),
        default=MissionStatus.PENDING,
        nullable=False,
        index=True,
    )
    current_stage: Mapped[str | None] = mapped_column(String(200), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_log: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
