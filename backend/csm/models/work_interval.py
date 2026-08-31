"""WorkInterval ORM model — append-only intervals of human / agent activity.

Two `kind` values are recognized:

- `human`  — a user-focused interval on the CSM UI. Opened by the first
  frontend heartbeat, extended by each subsequent heartbeat within the
  60s grace window, closed when the grace lapses (or on server shutdown /
  boot reap for orphans). `session_id` is always NULL.
- `agent`  — a per-Claude-session compute interval, opened on
  `message.user_sent` and closed on `message.assistant_done` / a terminal
  session event / the 30-min safety cap. `session_id` is the CSM row id.

Wall-clock accumulation (design choice 3=a): overlapping intervals from
different sessions each count fully — no union-collapsing. The consumer
computes totals with a straight `SUM(end_ts - start_ts)` per bucket.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class WorkIntervalKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"


class WorkIntervalSource(StrEnum):
    EVENT = "event"          # opened/closed by EventStream subscriber
    HEARTBEAT = "heartbeat"  # opened/closed by frontend heartbeat
    REAP = "reap"            # closed by lifespan boot-time orphan sweep


class WorkInterval(Base):
    __tablename__ = "work_interval"
    __table_args__ = (
        Index("ix_wi_kind_start", "kind", "start_ts"),
        Index("ix_wi_session_start", "session_id", "start_ts"),
        Index("ix_wi_open", "end_ts"),  # partial-index proxy: NULL sits at one end
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kind: Mapped[WorkIntervalKind] = mapped_column(
        SAEnum(WorkIntervalKind, native_enum=False), nullable=False
    )
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source: Mapped[WorkIntervalSource] = mapped_column(
        SAEnum(WorkIntervalSource, native_enum=False),
        nullable=False,
        default=WorkIntervalSource.EVENT,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive)
