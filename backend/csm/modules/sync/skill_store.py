"""Persisting a skill bundle into `skill_file` rows.

Split out of `service.py` because both the sync service and the API layer
need it, and out of `bundle.py` because that module is deliberately
ORM-free (it is imported by the adapters, which must not depend on the DB).

The bundle is replaced wholesale rather than diffed: a skill's file set is
small, and a wholesale replace makes "the row now matches what's on disk"
trivially true. Diffing would buy nothing and could leave an orphan behind.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from csm.models.skill import Skill, SkillFile
from csm.modules.sync.bundle import BundleFile, sha256_bytes, validate_rel_path


def _normalise(item: Any) -> tuple[str, bytes, int]:
    """Accept a `BundleFile`, or the dict shape the API and DB round-trip."""
    if isinstance(item, BundleFile):
        return item.rel_path, item.content, item.mode
    content = item["content"]
    if isinstance(content, str):
        content = content.encode("utf-8")
    return str(item["rel_path"]), content, int(item.get("mode", 0o644))


async def replace_skill_files(
    session: AsyncSession, skill: Skill, files: list[Any]
) -> list[SkillFile]:
    """Make `skill`'s bundle rows exactly `files`. Caller commits.

    `rel_path` is re-validated here even though the walker already did it:
    this is also the path taken by `PUT /api/sync/skills/{id}`, where the
    input is whatever the client sent.
    """
    await session.execute(delete(SkillFile).where(SkillFile.skill_id == skill.id))

    rows: list[SkillFile] = []
    seen: set[str] = set()
    for item in files or []:
        rel_path, content, mode = _normalise(item)
        rel_path = validate_rel_path(rel_path)
        if rel_path in seen:
            raise ValueError(f"duplicate rel_path in bundle: {rel_path!r}")
        seen.add(rel_path)
        rows.append(
            SkillFile(
                skill_id=skill.id,
                rel_path=rel_path,
                content=content,
                mode=mode & 0o7777,
                sha256=sha256_bytes(content),
            )
        )
    session.add_all(rows)
    return rows


def bundle_files_of(skill: Skill) -> list[BundleFile]:
    """`skill.files` as `BundleFile`s. Requires the relationship be loaded."""
    return [
        BundleFile(rel_path=f.rel_path, content=f.content, mode=f.mode)
        for f in sorted(skill.files, key=lambda f: f.rel_path)
    ]


__all__ = ["bundle_files_of", "replace_skill_files"]
