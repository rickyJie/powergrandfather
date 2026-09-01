"""Skill bundle walk / hash / validate — the shared vocabulary for
"a skill is a directory, not a file".

Until 2026-08-30 skill sync moved exactly one file, `SKILL.md`. Every
skill that shipped a helper (`query.py`, `scripts/*.py`, `references/*.md`)
therefore landed on the target agent structurally incomplete, and the
drift poller reported green because it only asked whether the *directory*
existed. This module is the read half of the fix.

Three things here are load-bearing and easy to get wrong:

1. **Permission bits travel with the bytes.** `atomic_write_with_hash_guard`
   creates its temp file 0600 and `os.replace`s it into place, so a bundle
   written without an explicit chmod arrives non-executable. A `query.py`
   that can't be executed is exactly as broken as one that isn't there.

2. **Symlinks are followed on read, never created on write.** ~80% of the
   skills in a real `~/.claude/skills` are symlinks into a skill-book repo.
   We want their *content*, but the target tree must be self-contained —
   materialising a symlink would point the target agent back at a path that
   may not exist in its world.

3. **`rel_path` is validated twice.** Once here at ingest, once again at
   write time. Ingest-side validation alone would be trusting the DB, and
   the DB is reachable through the API.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import pathspec

# The file the skill itself is keyed on — carried in `Skill.body_md`, never
# in the bundle, so it can't be represented twice with two different bodies.
SKILL_MD = "SKILL.md"

# Name of the optional per-skill ignore file (gitignore syntax). Matched
# relative to the skill directory. Never itself synced.
IGNORE_FILE = ".csmsyncignore"

# Always-off: build artefacts and VCS metadata that no skill needs and
# that would otherwise dominate the byte count.
EXCLUDED_DIRS = frozenset({
    "__pycache__",
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd", ".so", ".o", ".lock"})
EXCLUDED_NAMES = frozenset({".DS_Store", "Thumbs.db", IGNORE_FILE})

# Caps. Exceeding either is reported, never silently truncated — a bundle
# that quietly dropped half its files would reproduce the original bug in
# a subtler form.
MAX_FILE_BYTES = 1024 * 1024      # 1 MiB per file
MAX_BUNDLE_FILES = 200            # per skill


class BundleTooLarge(ValueError):
    """A skill directory blew a cap. Carries the human-readable reason."""


@dataclass(frozen=True, slots=True)
class BundleFile:
    """One non-SKILL.md file in a skill bundle.

    `rel_path` is POSIX, relative to the skill dir, validated. `mode` holds
    the low 12 permission bits of the source.
    """

    rel_path: str
    content: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def validate_rel_path(rel_path: str) -> str:
    """Return the normalised POSIX rel_path, or raise ValueError.

    Rejects absolute paths, `..` traversal, empty segments, NUL bytes, and
    `SKILL.md` itself (which lives in `Skill.body_md`). Called at ingest AND
    at write time — see module docstring.
    """
    if not rel_path or not rel_path.strip():
        raise ValueError("empty rel_path")
    if "\x00" in rel_path:
        raise ValueError(f"rel_path contains NUL: {rel_path!r}")

    p = PurePosixPath(rel_path.replace(os.sep, "/"))
    if p.is_absolute():
        raise ValueError(f"rel_path must be relative: {rel_path!r}")
    parts = p.parts
    if any(seg in ("..", ".", "") for seg in parts):
        raise ValueError(f"rel_path must not contain '.' or '..': {rel_path!r}")
    normalised = str(p)
    if normalised == SKILL_MD:
        raise ValueError("SKILL.md belongs in Skill.body_md, not the bundle")
    return normalised


def resolve_within(root: Path, rel_path: str) -> Path:
    """Join `rel_path` onto `root` and prove the result stays inside it.

    Belt-and-braces on top of `validate_rel_path`: catches the case where a
    symlink *already on disk* inside the target dir would redirect the write
    out of the tree.
    """
    rel = validate_rel_path(rel_path)
    root_resolved = root.resolve()
    target = (root_resolved / rel).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise ValueError(f"rel_path escapes the skill directory: {rel_path!r}")
    return root_resolved / rel


def load_ignore_spec(skill_dir: Path) -> pathspec.PathSpec | None:
    """Parse `<skill_dir>/.csmsyncignore` (gitignore syntax), if present."""
    f = skill_dir / IGNORE_FILE
    try:
        if not f.is_file():
            return None
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    # pathspec renamed the factory in 1.0 and deprecated the old name; we
    # declare `pathspec>=0.12`, so accept whichever this install has.
    try:
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except (KeyError, ValueError, LookupError):
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)


def walk_skill_bundle(
    skill_dir: Path,
    *,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_files: int = MAX_BUNDLE_FILES,
) -> tuple[list[BundleFile], list[str]]:
    """Collect every bundle file under `skill_dir`.

    Returns `(files, skipped)` where `skipped` holds one human-readable line
    per omission (oversize file, unreadable file, ignore-file match is NOT
    reported — that one is intentional and would be noise).

    Follows symlinks, both for the skill dir itself (the common case: the
    whole skill is a symlink into a skill-book repo) and for entries within
    it. Raises `BundleTooLarge` when the file count cap is blown, because
    silently keeping the first 200 files would ship a broken bundle that
    looks complete.
    """
    root = Path(skill_dir)
    if not root.is_dir():
        return [], []

    spec = load_ignore_spec(root)
    files: list[BundleFile] = []
    skipped: list[str] = []

    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIRS)
        here = Path(dirpath)
        for fn in sorted(filenames):
            if fn in EXCLUDED_NAMES or Path(fn).suffix in EXCLUDED_SUFFIXES:
                continue
            src = here / fn
            rel = src.relative_to(root).as_posix()
            if rel == SKILL_MD:
                continue
            if spec is not None and spec.match_file(rel):
                continue
            try:
                st = src.stat()  # follows symlinks
            except OSError as e:
                skipped.append(f"{rel}: stat failed ({e.__class__.__name__})")
                continue
            if not os.path.isfile(src):
                continue
            if st.st_size > max_file_bytes:
                skipped.append(
                    f"{rel}: {st.st_size} bytes exceeds the "
                    f"{max_file_bytes}-byte per-file cap"
                )
                continue
            try:
                content = src.read_bytes()
            except OSError as e:
                skipped.append(f"{rel}: read failed ({e.__class__.__name__})")
                continue
            files.append(
                BundleFile(
                    rel_path=validate_rel_path(rel),
                    content=content,
                    mode=st.st_mode & 0o7777,
                )
            )

    if len(files) > max_files:
        raise BundleTooLarge(
            f"{root.name}: {len(files)} bundle files exceeds the {max_files}-file "
            f"cap. Add a {IGNORE_FILE} to the skill directory to exclude "
            f"generated artefacts."
        )

    files.sort(key=lambda f: f.rel_path)
    return files, skipped


def count_bundle_files(skill_dir: Path) -> int:
    """How many files the bundle has, without reading any of them.

    The UI's skill picker only needs the number. Reading content for that
    would mean pulling every skill's whole tree off disk on each render.
    """
    root = Path(skill_dir)
    if not root.is_dir():
        return 0
    spec = load_ignore_spec(root)
    n = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        here = Path(dirpath)
        for fn in filenames:
            if fn in EXCLUDED_NAMES or Path(fn).suffix in EXCLUDED_SUFFIXES:
                continue
            rel = (here / fn).relative_to(root).as_posix()
            if rel == SKILL_MD:
                continue
            if spec is not None and spec.match_file(rel):
                continue
            n += 1
    return n


def bundle_manifest(files: list[BundleFile]) -> dict[str, str]:
    """`{rel_path: sha256}` — what gets stored in `Skill.last_synced_files`."""
    return {f.rel_path: f.sha256 for f in files}


def bundle_hash(body_md: str, files: list[BundleFile]) -> str:
    """Stable hash over the whole skill: SKILL.md plus every bundle file.

    Covers `mode` as well as content, so losing the exec bit registers as
    drift rather than passing as identical.
    """
    h = hashlib.sha256()
    h.update(sha256_bytes(body_md.encode("utf-8")).encode())
    for f in sorted(files, key=lambda x: x.rel_path):
        h.update(b"\0")
        h.update(f.rel_path.encode("utf-8"))
        h.update(f"\0{f.mode:o}\0".encode())
        h.update(f.sha256.encode())
    return h.hexdigest()


def bundle_hash_from_manifest(body_md: str, manifest: dict[str, Any]) -> str:
    """`bundle_hash` for callers that hold a `{rel_path: (sha, mode)}` map.

    Accepts either `{rel_path: sha}` (mode assumed 0o644, the ingest default)
    or `{rel_path: {"sha256": ..., "mode": ...}}`.
    """
    h = hashlib.sha256()
    h.update(sha256_bytes(body_md.encode("utf-8")).encode())
    for rel in sorted(manifest):
        entry = manifest[rel]
        if isinstance(entry, dict):
            sha, mode = entry.get("sha256", ""), int(entry.get("mode", 0o644))
        else:
            sha, mode = str(entry), 0o644
        h.update(b"\0")
        h.update(rel.encode("utf-8"))
        h.update(f"\0{mode:o}\0".encode())
        h.update(sha.encode())
    return h.hexdigest()


__all__ = [
    "EXCLUDED_DIRS",
    "EXCLUDED_NAMES",
    "EXCLUDED_SUFFIXES",
    "IGNORE_FILE",
    "MAX_BUNDLE_FILES",
    "MAX_FILE_BYTES",
    "SKILL_MD",
    "BundleFile",
    "BundleTooLarge",
    "bundle_hash",
    "bundle_hash_from_manifest",
    "bundle_manifest",
    "count_bundle_files",
    "load_ignore_spec",
    "resolve_within",
    "sha256_bytes",
    "validate_rel_path",
    "walk_skill_bundle",
]
