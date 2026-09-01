"""Filesystem-convention skill I/O, shared by every adapter whose CLI
discovers skills as `<skills_dir>/<name>/SKILL.md`.

claude, codex and the gemini mock all use the identical on-disk layout, and
before this module each carried its own byte-for-byte copy of the read and
write logic. That duplication is how `write_simple_skill` ended up shipping
only SKILL.md in three places at once.

Everything here takes `skills_dir` as an argument, so it stays a plain
function library — no adapter base class, no MRO to reason about.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from csm.modules.sync.atomic_write import atomic_write_with_hash_guard
from csm.modules.sync.bundle import (
    SKILL_MD,
    BundleFile,
    bundle_hash,
    resolve_within,
    walk_skill_bundle,
)
from csm.modules.sync.errors import ExternalSkillSource

log = logging.getLogger(__name__)

SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def guard_skill_name(name: str) -> None:
    """Reject anything that isn't a bare, lowercase directory name."""
    if not SKILL_NAME_RE.match(name or ""):
        raise ValueError(f"invalid skill name: {name!r}")


def skill_dir_for(skills_dir: Path, name: str) -> Path:
    """`<skills_dir>/<name>`, name-guarded and proven to stay inside root.

    Does NOT resolve the leaf: callers need to see the symlink itself
    (see `assert_not_external`), and `.resolve()` would hide it.
    """
    guard_skill_name(name)
    root = Path(skills_dir).resolve()
    target = root / name
    # `name` is already regex-guarded so this can't traverse, but the check
    # is cheap and survives a future loosening of the regex.
    if target.parent != root:
        raise ValueError(f"skill name resolves outside skills_dir: {name}")
    return target


def assert_not_external(skill_path: Path, name: str) -> None:
    """Raise `ExternalSkillSource` if the skill dir is a symlink.

    In practice most of a user's `~/.claude/skills/*` are symlinks into a
    skill-book repo. `os.replace()` and `shutil.rmtree()` both follow them,
    so writing or pruning through one silently edits a git working tree.
    """
    if skill_path.is_symlink():
        try:
            target = skill_path.resolve()
        except OSError:
            target = Path(os.readlink(skill_path))
        raise ExternalSkillSource(path=skill_path, target=target, name=name)


# ---- read ------------------------------------------------------------


def extract_description(skill_md: Path) -> str:
    """Read `description:` from SKILL.md's YAML frontmatter (best-effort).

    Returns "" when missing or malformed — list_skills is best-effort
    metadata for the UI, not something we want to raise on.
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    body_after = text[3:]
    end_idx = body_after.find("\n---")
    if end_idx == -1:
        return ""
    frontmatter = body_after[:end_idx]
    for line in frontmatter.splitlines():
        if line.strip().startswith("description:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def list_skills(
    skills_dir: Path | None, *, skip_dot_dirs: bool = False
) -> list[dict[str, Any]]:
    """Enumerate `<name>/SKILL.md` under `skills_dir` — metadata only.

    `skip_dot_dirs` excludes dot-prefixed directories; codex ships its
    built-in skills under `.system/` and those must never be adopted into
    CSM as user skills.
    """
    if skills_dir is None or not Path(skills_dir).is_dir():
        return []
    out: list[dict[str, Any]] = []
    for child in sorted(Path(skills_dir).iterdir()):
        if not child.is_dir():  # follows symlinks — intentional
            continue
        if skip_dot_dirs and child.name.startswith("."):
            continue
        skill_md = child / SKILL_MD
        if not skill_md.is_file():
            continue
        out.append({
            "name": child.name,
            "path": str(skill_md),
            "description": extract_description(skill_md),
        })
    return out


def list_skills_full(
    skills_dir: Path | None, *, skip_dot_dirs: bool = False
) -> list[dict[str, Any]]:
    """`list_skills()` plus body_md, file_count and bundle_hash.

    Bundle *bytes* are never returned: this runs on every agent tick and a
    skill can carry a hundred files. `bundle_hash` is enough to tell "same"
    from "different"; `read_skill_bundle()` fetches content when something
    actually needs it.

    Callers that only want a count (UI listings) should use
    `bundle.count_bundle_files()` instead — this one reads every file.
    """
    out: list[dict[str, Any]] = []
    for entry in list_skills(skills_dir, skip_dot_dirs=skip_dot_dirs):
        body_md = ""
        try:
            body_md = Path(entry["path"]).read_text(encoding="utf-8")
        except (OSError, KeyError):
            pass
        skill_root = Path(entry["path"]).parent
        try:
            files, _skipped = walk_skill_bundle(skill_root)
        except Exception:
            # A too-large / unreadable bundle must not take down the whole
            # enumeration — the agent tick needs the other skills.
            log.exception("bundle walk failed for %s", skill_root)
            files = []
        out.append({
            **entry,
            "body_md": body_md,
            "file_count": len(files),
            "bundle_hash": bundle_hash(body_md, files),
        })
    return out


def read_skill_bundle(skills_dir: Path | None, name: str) -> dict[str, Any] | None:
    """Read one skill in full — SKILL.md body plus every bundle file."""
    if skills_dir is None:
        return None
    skill_path = skill_dir_for(Path(skills_dir), name)
    skill_md = skill_path / SKILL_MD
    if not skill_md.is_file():
        return None
    body_md = skill_md.read_text(encoding="utf-8", errors="replace")
    files, skipped = walk_skill_bundle(skill_path)
    return {
        "name": name,
        "description": extract_description(skill_md),
        "body_md": body_md,
        "files": files,
        "skipped": skipped,
    }


# ---- write -----------------------------------------------------------


def _coerce_files(raw: Any) -> list[BundleFile]:
    """Accept `BundleFile`s or plain dicts (the API / DB shape)."""
    out: list[BundleFile] = []
    for item in raw or []:
        if isinstance(item, BundleFile):
            out.append(item)
            continue
        content = item["content"]
        if isinstance(content, str):
            content = content.encode("utf-8")
        out.append(
            BundleFile(
                rel_path=str(item["rel_path"]),
                content=content,
                mode=int(item.get("mode", 0o644)),
            )
        )
    return out


def write_skill_bundle(skills_dir: Path | None, spec: dict[str, Any]) -> dict[str, Any]:
    """Materialise a full skill directory. See `CLIAdapter.write_skill_bundle`.

    Idempotent: two consecutive calls with the same spec leave a
    byte-identical tree (contract in
    `docs/backends/adapter_idempotency_contract.md`).
    """
    if skills_dir is None:
        raise NotImplementedError("SYNC_SKILLS not supported by this adapter")

    name = str(spec["name"])
    skill_path = skill_dir_for(Path(skills_dir), name)
    assert_not_external(skill_path, name)

    body_md = str(spec["body_md"])
    files = _coerce_files(spec.get("files"))
    # Keep filelock's persistent sidecar outside the skill directory. Newer
    # filelock releases deliberately retain lock files after release; placing
    # the sidecar beside SKILL.md makes it part of the bundle on the next read.
    # One hidden lock per skill preserves the existing serialization contract
    # without leaking CSM bookkeeping into the materialised skill.
    skill_lock = skill_path.parent / f".{name}.csm-sync.lock"

    written: list[str] = []

    # SKILL.md first: if we die partway, the skill is at worst back to the
    # old single-file shape rather than a bundle with no entry point.
    atomic_write_with_hash_guard(
        skill_path / SKILL_MD,
        body_md.encode("utf-8"),
        lock_path=skill_lock,
    )
    written.append(SKILL_MD)

    for f in files:
        target = resolve_within(skill_path, f.rel_path)
        atomic_write_with_hash_guard(target, f.content, lock_path=skill_lock)
        # atomic_write creates its temp file 0600 and renames it into place,
        # so the mode has to be (re)applied every time — not just on create.
        os.chmod(target, f.mode & 0o7777)
        written.append(f.rel_path)

    pruned = _prune(skill_path, spec.get("prune"), keep={f.rel_path for f in files})
    return {"written": written, "pruned": pruned}


def _prune(skill_path: Path, previous: Any, *, keep: set[str]) -> list[str]:
    """Delete files we wrote last time that aren't in the bundle any more.

    Scoped to `previous` — the manifest CSM itself last wrote to this agent.
    Deleting "everything not in the new manifest" would take out files the
    user dropped in by hand, which is not ours to do.
    """
    pruned: list[str] = []
    for rel in sorted(dict(previous or {})):
        if rel in keep or rel == SKILL_MD:
            continue
        try:
            target = resolve_within(skill_path, rel)
        except ValueError:
            log.warning("prune: refusing suspicious rel_path %r", rel)
            continue
        if target.is_symlink() or not target.is_file():
            # Never unlink through a symlink, and don't chase a path that
            # has since become a directory.
            continue
        try:
            target.unlink()
            pruned.append(rel)
        except OSError:
            log.exception("prune: failed to unlink %s", target)
            continue
        _prune_empty_parents(skill_path, target.parent)
    return pruned


def _prune_empty_parents(root: Path, leaf: Path) -> None:
    """Walk up from `leaf` removing now-empty dirs, stopping at `root`."""
    root = root.resolve()
    cur = leaf
    while cur != root and root in cur.resolve().parents:
        try:
            next(cur.iterdir())
            return  # not empty
        except StopIteration:
            pass
        except OSError:
            return
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent


def remove_skill(skills_dir: Path | None, name: str) -> None:
    """Delete `<skills_dir>/<name>/` recursively. Idempotent."""
    if skills_dir is None:
        return
    skill_path = skill_dir_for(Path(skills_dir), name)
    assert_not_external(skill_path, name)
    if not skill_path.is_dir():
        return
    shutil.rmtree(skill_path)


__all__ = [
    "SKILL_NAME_RE",
    "assert_not_external",
    "extract_description",
    "guard_skill_name",
    "list_skills",
    "list_skills_full",
    "read_skill_bundle",
    "remove_skill",
    "skill_dir_for",
    "write_skill_bundle",
]
