"""Skill ORM model — one row per CSM-managed skill.

Skill *content* is rendered on disk under
`<adapter.skills_dir()>/<name>/` at sync time. The DB row carries:
- `body_md`: the full SKILL.md file content (must start with YAML
  frontmatter `---` per spec §7 validation);
- `description`: the trigger-snippet shown in the UI list;
- `share_scope`: which adapters this skill materialises on;
- `files`: the *bundle* — every sibling file next to SKILL.md
  (`query.py`, `references/*.md`, `scripts/*.py`, ...). See `SkillFile`.

`name` matches `^[a-z0-9][a-z0-9-]{0,63}$` — same rule as Instruction —
because it becomes a filesystem directory name.

**Bundle sync (2026-08-30).** Before this, sync only ever materialised
`SKILL.md`, so any skill whose SKILL.md said "run ./query.py" arrived
broken on every non-source agent. The DB is the single source of truth
for the whole bundle (ADR-0002): file bytes live in `skill_file`, not
on a remembered source path, because `fanout_ledger` crash-recovery and
`last_synced_hashes` both assume the row alone can reconstruct the
target.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from csm.models.base import Base
from csm.utils.time import now_utc_naive


class Skill(Base):
    __tablename__ = "skill"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    body_md: Mapped[str] = mapped_column(Text, nullable=False)

    share_scope: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_utc_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=now_utc_naive, onupdate=now_utc_naive, nullable=False,
    )

    # ---- sync v2 agent-driven (P1 migration m5n6o7p8q9rs) ----
    origin: Mapped[str] = mapped_column(
        Text, default="csm", server_default="csm", nullable=True,
    )
    last_synced_hashes: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=True,
    )

    # ---- bundle sync (migration w6x7y8z9a105) ----
    # {agent_name: {rel_path: sha256}} — the manifest we last wrote to that
    # agent. Prune on the next push is scoped to exactly these paths, so a
    # file the user dropped into the target dir by hand is never collateral.
    last_synced_files: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}", nullable=True,
    )

    files: Mapped[list[SkillFile]] = relationship(
        "SkillFile",
        back_populates="skill",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="SkillFile.rel_path",
    )


class SkillFile(Base):
    """One non-SKILL.md file belonging to a skill bundle.

    `rel_path` is POSIX-relative to the skill directory (`scripts/run.py`),
    never absolute and never containing `..` — enforced at both the ingest
    and the write side (`csm.modules.sync.bundle`).

    `mode` holds the low 12 permission bits of the source file. It is stored
    because `atomic_write_with_hash_guard` creates its temp file 0600, so a
    bundle written without an explicit chmod loses the executable bit — and
    a `query.py` that isn't executable is exactly as broken as a missing one.
    """

    __tablename__ = "skill_file"
    __table_args__ = (UniqueConstraint("skill_id", "rel_path", name="uq_skill_file_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    skill_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("skill.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    rel_path: Mapped[str] = mapped_column(String(512), nullable=False)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    mode: Mapped[int] = mapped_column(Integer, default=0o644, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    skill: Mapped[Skill] = relationship("Skill", back_populates="files")
