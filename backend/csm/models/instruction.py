"""Instruction ORM model — a single memory-module marker block.

Each row becomes exactly one `<!-- csm:start id=<name> -->` block inside
`~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` (per adapter's
`memory_paths("user")`). `name` doubles as the marker id, so it must
match `^[a-z0-9][a-z0-9-]{0,63}$` — validated at the API layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class Instruction(Base):
    __tablename__ = "instruction"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # UNIQUE + doubles as marker id in the on-disk memory file.
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    # JSON list of adapter names authorised to receive this block.
    share_scope: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    # Ordering hint within CLAUDE.md / AGENTS.md (higher first).
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc_naive, onupdate=now_utc_naive, nullable=False,
    )

    # ---- sync v2 agent-driven (P1 migration m5n6o7p8q9rs) ----
    # `origin`: provenance — 'csm' (user-authored via UI) or
    # 'agent_adopt:<agent>' (adopted by SyncAgent from that CLI).
    origin: Mapped[str] = mapped_column(
        Text, default="csm", server_default="csm", nullable=True,
    )
    # `last_synced_hashes`: per-enrolled-agent last-known body hash OR
    # sentinel ('UNSUPPORTED' | 'UNKNOWN' | 'DIVERGED:<hex>').
    # See csm.modules.sync.sentinels for semantics.
    last_synced_hashes: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=True,
    )
