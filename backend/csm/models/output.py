"""Output ORM model — an artifact discovered after a Run finishes."""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class OutputType(StrEnum):
    FILE = "file"
    LOG = "log"
    MARKDOWN = "markdown"
    URL = "url"
    JSON = "json"


class Output(Base):
    __tablename__ = "output"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    path: Mapped[str] = mapped_column(String(1024))
    type: Mapped[OutputType] = mapped_column(SAEnum(OutputType, native_enum=False), default=OutputType.FILE)
    preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
