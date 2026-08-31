"""Sync-subsystem exceptions.

Three families, all used to fence a single sync attempt:

- `SyncPreflightError` — raised BEFORE we invoke the CLI, when the request
  can't proceed at all (missing `${VAR}` from B5 env expansion is the
  canonical case). The service converts this to HTTP 400.
- `ConcurrentWriteDrift` — raised AFTER a successful atomic write, when the
  post-write hash check shows a concurrent writer (typically the claude
  REPL) clobbered our block. The service records a drift row and returns
  `SyncStatus.SKIPPED`; it must NOT retry inside the same tick.
- `ExternalSkillSource` — raised INSTEAD of writing, when a skill directory
  turns out to be a symlink to content CSM doesn't own. Also a SKIPPED, but
  it will never clear on its own; the user has to decide.
"""
from __future__ import annotations

from pathlib import Path


class SyncPreflightError(Exception):
    """Sync cannot start because a required input is missing/invalid.

    B5 uses this for undefined `${VAR}` refs. Attribute `missing` carries
    the sorted list of variable NAMES (never values, per B5 side-channel
    rule). API layer surfaces the names — never the values — in HTTP 400.
    """

    def __init__(self, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing: list[str] = list(missing or [])


class ConcurrentWriteDrift(Exception):
    """A concurrent writer overwrote our atomic write.

    Raised by `atomic_write_with_hash_guard()`. Caller (SyncService)
    catches, records a `drift_record`, and returns SyncStatus.SKIPPED.
    Drift poll will re-reconcile on the next tick — DO NOT retry here.
    """

    def __init__(
        self,
        *,
        path: Path,
        expected_hash: str,
        actual_hash: str,
        pre_hash: str,
    ) -> None:
        super().__init__(
            f"concurrent write drift at {path}: "
            f"expected {expected_hash[:12]}… got {actual_hash[:12]}…"
        )
        self.path: Path = path
        self.expected_hash: str = expected_hash
        self.actual_hash: str = actual_hash
        self.pre_hash: str = pre_hash


class ExternalSkillSource(Exception):
    """The target skill directory is a symlink out of `skills_dir()`.

    In a real setup most of `~/.claude/skills/*` are symlinks into a
    skill-book git repo. `os.replace()` and `shutil.rmtree()` both follow
    those links, so a naive write would edit — and a prune would delete —
    files in the user's working tree, with no record that CSM did it.

    The adapter refuses instead. The caller records a drift row with
    `DriftReason.EXTERNAL_SOURCE` and returns SyncStatus.SKIPPED, which
    surfaces in the UI as a durable "not syncing, and here's why" rather
    than silent repo mutation.
    """

    def __init__(self, *, path: Path, target: Path, name: str) -> None:
        super().__init__(
            f"skill {name!r} at {path} is a symlink to {target} — refusing to "
            f"write through it (content CSM does not own)"
        )
        self.path: Path = path
        self.target: Path = target
        self.name: str = name


__all__ = ["SyncPreflightError", "ConcurrentWriteDrift", "ExternalSkillSource"]
