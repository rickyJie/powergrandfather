"""FileState ORM model — incremental tail offsets for adapter artifact files.

Renamed from the pre-refactor `jsonl_path` schema: v2 adds an `agent` column
so per-adapter tail state is namespaced (claude and codex both write to
this table under their respective artifact roots), and `jsonl_path` was
renamed to `artifact_path` since codex writes `rollout-*.jsonl`, not
"JSONL in general".

`artifact_path` remains the PK because claude's artifact root
(`~/.claude/projects/`) and codex's (`~/.codex/sessions/`) never overlap;
paths are globally unique. A composite `(artifact_path, agent)` PK was
considered but rejected as needless complexity for a non-existent
conflict.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class FileState(Base):
    __tablename__ = "file_state"

    artifact_path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    # Which adapter owns this tail state row. Multi-agent v2 addition; old
    # rows backfill to 'claude' via migration server_default.
    agent: Mapped[str] = mapped_column(
        String(32), nullable=False, default="claude",
        server_default="claude", index=True,
    )
    last_offset: Mapped[int] = mapped_column(BigInteger, default=0)
    last_mtime: Mapped[float] = mapped_column(Float, default=0.0)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    last_ctx_tokens: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, onupdate=now_utc_naive)
