"""WorkflowDefinition ORM model — compiled, reviewed M8 workflow template."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class WorkflowReviewStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    REJECTED = "rejected"
    ERROR = "error"


class WorkflowDefinition(Base):
    __tablename__ = "workflow_definition"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    file_path: Mapped[str] = mapped_column(String(1024))
    yaml_content: Mapped[str] = mapped_column(Text)
    compiled_rules: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    review_status: Mapped[WorkflowReviewStatus] = mapped_column(
        SAEnum(WorkflowReviewStatus, native_enum=False),
        default=WorkflowReviewStatus.PENDING,
        nullable=False,
    )
    review_report: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Soft-delete (纯软删, no un-archive endpoint). Rows with a non-null
    # archived_at are hidden from `GET /api/workflows` by default; past
    # missions still reference them so we cannot hard-delete without
    # breaking history joins.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # Optional user-created bucket. NULL means the workflow falls back to
    # the auto-derived group (last segment of the `repo_root` parameter
    # default). When set, the frontend groups by the linked Project name.
    project_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
