"""AgentDefinition + AgentConversation ORM models.

AgentDefinition holds the preconfigured template (cwd + cached system prompt).
AgentConversation is the runtime record linking a user-spawned chat to its
underlying Session row.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class AgentDefinition(Base):
    __tablename__ = "agent_definition"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200))
    icon: Mapped[str | None] = mapped_column(String(64), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cwd: Mapped[str] = mapped_column(String(1024))
    # Where the prompt was loaded from at create-time; informational only —
    # the cached body is what gets injected on every spawn.
    prompt_source: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    prompt_cached: Mapped[str] = mapped_column(Text)
    # When True, spawn passes `--disallowedTools Skill` so the agent's claude
    # session cannot invoke any installed Claude Code Skill. Lets users build
    # narrowly-scoped agents that never reach for /skill-driven behavior.
    disable_skills: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc_naive, onupdate=now_utc_naive
    )


class AgentConversation(Base):
    __tablename__ = "agent_conversation"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_def_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("agent_definition.id"), index=True
    )
    session_id: Mapped[str] = mapped_column(String(36), ForeignKey("session.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
    last_activity_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
