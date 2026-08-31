"""SessionProject ORM model — user-managed grouping for interactive sessions.

Deliberately separate from `workflow.Project` (which groups workflow
templates). Rationale: the two concepts drift — a "workflow project" is
authoring metadata, a "session project" is a per-session UX label the
user can rename / archive without touching workflow config. Keeping them
as sibling tables also lets each evolve independently.

When a `Session.session_project_id` is null, Sessions.vue synthesises a
virtual "auto: <cwd 2-level>" group on the fly — no need for the DB to
pretend cwds are first-class projects.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class SessionProject(Base):
    __tablename__ = "session_project"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
