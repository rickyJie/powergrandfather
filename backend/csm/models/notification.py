"""Notification ORM model — persisted notifications managed by NotificationBus."""
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


class NotificationType(StrEnum):
    NEW_MESSAGE = "new_message"
    AUTO_NEEDS_REVIEW = "auto_needs_review"
    SESSION_CRASHED = "session_crashed"
    AUTO_RUN_FAILED = "auto_run_failed"
    TOKEN_WARNING = "token_warning"
    PORT_CONFLICT = "port_conflict"
    # A workflow mission reached a terminal state (succeeded or failed).
    # AUTO_RUN_FAILED is for single AUTO sessions that exited non-zero;
    # this one is at the mission (multi-stage) level. Cancellation does not
    # emit a notification — that's user-initiated.
    MISSION_DONE = "mission_done"


class Notification(Base):
    __tablename__ = "notification"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    type: Mapped[NotificationType] = mapped_column(SAEnum(NotificationType, native_enum=False))
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dismissed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    channels_sent: Mapped[list[str]] = mapped_column(JSON, default=list)
    notif_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
