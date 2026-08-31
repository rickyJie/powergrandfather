"""SessionFileTouch — one row per `Write`/`Edit`/`MultiEdit`/`Create` tool
invocation observed for a session.

Populated by `csm.api.hooks` on PreToolUse; queried by
`csm.api.files.recent(sid)` to power the "📄 Files (N)" popover in the
session header. Cascade-deleted with the parent Session.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class SessionFileTouch(Base):
    __tablename__ = "session_file_touch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    sid: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("session.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    path: Mapped[str] = mapped_column(String(2048), nullable=False)
    tool: Mapped[str] = mapped_column(String(32), nullable=False)
    ts: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, nullable=False)

    # Composite index to serve "recent N files for session X" queries in a
    # single index scan (order-by ts desc). Standalone index on `sid` above
    # is redundant with the leading column here but kept for FK back-refs.
    __table_args__ = (
        Index("ix_session_file_touch_sid_ts", "sid", "ts"),
    )
