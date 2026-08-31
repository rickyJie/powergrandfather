"""Single-instance guard for the SQLite datastore.

ADR-0002 pins CSM to a **single FastAPI process + single SQLite file**. When
two processes accidentally boot against the *same* `csm.db` (e.g. the desktop
`scripts/start.sh` and the mobile `start_with_mobile.sh` both defaulting to
`./csm.db`), each spins up its own EventStream tailing the same JSONL corpus
and its own NotificationBus. They then race on the shared DB: NEW_MESSAGE
rows, unread counts, and `assistant_done_rebind` mutations to
`Session.external_session_id` get processed twice with only *per-process*
in-memory dedup. Whichever process wins a given turn's DB write suppresses the
other's WS push, so the browser tab connected to the "losing" process silently
stops seeing new messages. This failure is invisible in the logs unless you
know to look for two processes cross-suppressing the same session id.

The root fix is to make a second owner of the same DB **impossible to start**
rather than silently harmful: acquire an exclusive advisory lock (`flock`) on
`<db_path>.lock` at boot. A second process refuses to start and names the PID
holding the lock. `flock` is tied to the open file description and is released
automatically when the holder dies — even on SIGKILL — so there is no stale
pidfile to clean up.

Distinct DB paths take distinct lock files, so running a mobile instance
against `CSM_DB_PATH=/tmp/pgf-mobile.db` alongside the desktop is still fine —
they don't share state, so they don't contend.
"""
from __future__ import annotations

import fcntl
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)


class SingleInstanceError(RuntimeError):
    """Raised when another live process already owns this DB's lock."""

    def __init__(self, lock_path: Path, holder: str) -> None:
        self.lock_path = lock_path
        self.holder = holder
        super().__init__(
            f"another CSM instance (pid={holder}) already owns {lock_path.parent / lock_path.name.removesuffix('.lock')}; "
            f"refusing to start a second backend against the same SQLite file. "
            f"Stop the other process first (e.g. `kill {holder}`) or point this "
            f"one at a different DB via CSM_DB_PATH=... "
            f"(see docs/decisions/0002-single-process-monolith.md)."
        )


class DbInstanceLock:
    """Exclusive advisory lock on `<db_path>.lock`.

    `acquire()` retries briefly so a `--reload` worker handover (old worker
    releasing while the new one starts) doesn't spuriously trip the guard; a
    genuine second instance still fails once the window elapses.
    """

    def __init__(self, db_path: Path) -> None:
        # Sibling of the DB file so the lock travels with the datastore and
        # distinct DB paths get distinct locks.
        self._lock_path = db_path.with_name(db_path.name + ".lock")
        self._fd: int | None = None

    @property
    def lock_path(self) -> Path:
        return self._lock_path

    def acquire(self, timeout: float = 3.0, interval: float = 0.1) -> None:
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    holder = self._read_holder(fd)
                    os.close(fd)
                    raise SingleInstanceError(self._lock_path, holder)
                time.sleep(interval)
        # Won the lock — stamp our pid so a would-be second instance can name us.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}\n".encode())
            os.fsync(fd)
        except OSError:  # diagnostics only; a write failure must not break boot
            log.warning("could not stamp pid into %s", self._lock_path)
        self._fd = fd
        log.info("acquired single-instance DB lock %s (pid=%s)", self._lock_path, os.getpid())

    def _read_holder(self, fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            data = os.read(fd, 64).decode(errors="replace").strip()
            return data or "<unknown>"
        except OSError:
            return "<unknown>"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(self._fd)
        except OSError:
            pass
        self._fd = None


__all__ = ["DbInstanceLock", "SingleInstanceError"]
