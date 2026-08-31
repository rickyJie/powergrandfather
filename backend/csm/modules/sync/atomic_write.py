"""B1 · Write-hash-compare atomic write guard.

Python `fcntl.flock` cannot exclude a Node.js `rename` — the Claude REPL
uses "write tmp → atomic rename over target" and each rename swaps the
inode, which is invisible to fcntl locks. This module compensates by
verifying the post-write hash and raising `ConcurrentWriteDrift` when the
target didn't end up with our bytes.

The caller (SyncService) reacts by recording a drift row and returning
SyncStatus.SKIPPED — the drift poll worker will retry on the next tick.
DO NOT retry here (retry loops mask real drift and can pin a CPU during
a busy writer).
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from filelock import FileLock

from csm.modules.sync.errors import ConcurrentWriteDrift


def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _read_or_empty(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return b""


def atomic_write_with_hash_guard(
    path: Path,
    new_content: bytes,
    *,
    lock_path: Path | None = None,
    lock_timeout: float = 5.0,
) -> None:
    """Atomic write followed by a post-write hash check.

    Steps:
      1. Acquire an in-process file lock (protects CSM internal writers).
      2. Snapshot pre-hash (drift diagnostics only — does NOT gate the write).
      3. Write to a `NamedTemporaryFile` in the SAME directory, then
         `os.replace()` over `path` (atomic on POSIX).
      4. Re-read `path` and compare its hash to `sha256(new_content)`.

    Raises `ConcurrentWriteDrift` when step 4 doesn't match. Caller records
    the drift and skips this sync attempt.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_target = lock_path or path.with_suffix(path.suffix + ".lock")
    lock = FileLock(str(lock_target), timeout=lock_timeout)
    expected_hash = _sha256_bytes(new_content)

    with lock:
        pre_hash = _sha256_bytes(_read_or_empty(path))

        tmp_fd, tmp_name = tempfile.mkstemp(
            prefix=".csm_sync_",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(new_content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

        actual_hash = _sha256_bytes(_read_or_empty(path))
        if actual_hash != expected_hash:
            raise ConcurrentWriteDrift(
                path=path,
                expected_hash=expected_hash,
                actual_hash=actual_hash,
                pre_hash=pre_hash,
            )


__all__ = ["atomic_write_with_hash_guard"]
