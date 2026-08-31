"""PTY-backed subprocess wrapper used to host `claude` CLI sessions.

Built on `ptyprocess.PtyProcess`. Sync I/O is wrapped by callers with
`asyncio.to_thread` (PtyProcess does not provide async primitives).
"""
from __future__ import annotations

import os
import select
import signal
import time
from pathlib import Path

from ptyprocess import PtyProcess  # type: ignore


class PtyHandle:
    """Async-friendly wrapper around `ptyprocess.PtyProcess`.

    `ptyprocess` is sync-only, so every method here is sync and callers
    (notably `SessionManager`) wrap them with `asyncio.to_thread`.

    `terminate(graceful, grace_sec)` implements the architecture-prescribed
    SIGINT → SIGTERM → SIGKILL ladder (`graceful=True`) or just SIGKILL
    (`graceful=False`). Always closes the underlying process; idempotent.

    `read(max_bytes)` returns `b""` on EOF or on any OSError — callers
    interpret a sustained empty read combined with `is_alive() == False`
    as "process exited".

    `exit_code` is `None` until the child has exited.
    """

    def __init__(self, proc: PtyProcess):
        self._proc = proc

    @property
    def pid(self) -> int:
        return self._proc.pid

    def fileno(self) -> int:
        """Master-side PTY fd. Callers use it with asyncio's selector
        (`loop.add_reader`) to stream output without holding a thread."""
        return self._proc.fileno()

    def is_alive(self) -> bool:
        return self._proc.isalive()

    def read(self, max_bytes: int = 8192, timeout: float = 0.05) -> bytes:
        """Bounded-wait read up to max_bytes. Returns b'' on timeout or EOF.

        `ptyprocess.PtyProcess.read` is a plain blocking `os.read` — if the PTY
        has no output, the thread hosting this call (typically an
        `asyncio.to_thread` worker in `SessionManager._reader_loop`) parks
        indefinitely and holds a slot in the default asyncio ThreadPoolExecutor.
        With N idle sessions, N threads are stuck, and unrelated `to_thread`
        calls (notably PTY *writes* for user keystrokes) queue behind them —
        which the user experiences as input lag.

        Gating the read with `select` bounds the block to `timeout` seconds
        so the worker always returns quickly, whether or not data arrived.
        """
        try:
            fd = self._proc.fileno()
        except Exception:
            return b""
        try:
            r, _, _ = select.select([fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not r:
            return b""
        try:
            data = self._proc.read(max_bytes)
            if isinstance(data, str):
                # ptyprocess returns str by default; ensure bytes.
                return data.encode("utf-8", errors="replace")
            return data
        except EOFError:
            return b""
        except OSError:
            return b""

    def write(self, data: bytes) -> int:
        """Write to master PTY; handles the fd being in non-blocking mode.

        The master fd is put into non-blocking mode by `spawn()` so that
        SessionManager's asyncio-native reader (`loop.add_reader` +
        non-blocking `os.read`) doesn't stall the event loop. That means
        writes can raise `BlockingIOError` when the kernel-side TTY buffer
        is full (rare, but possible for large `initial_prompt` blobs). We
        loop with `select` to drain in that case.
        """
        if not isinstance(data, bytes):
            data = bytes(data)
        try:
            fd = self._proc.fileno()
        except Exception:
            return 0
        total = 0
        deadline = time.monotonic() + 5.0
        while total < len(data):
            try:
                n = os.write(fd, data[total:])
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return total
                _, w, _ = select.select([], [fd], [], remaining)
                if not w:
                    return total
                continue
            except (OSError, EOFError):
                return total
            if n <= 0:
                return total
            total += n
        return total

    def setwinsize(self, rows: int, cols: int) -> None:
        try:
            self._proc.setwinsize(rows, cols)
        except Exception:
            pass

    def terminate(self, graceful: bool = True, grace_sec: int = 5) -> int | None:
        """Send SIGINT, wait, then SIGTERM, wait, then SIGKILL. Returns exit code.

        Return value contract (B1 fix):
        - clean exit (process called `exit(n)` / returned from main) → n
        - killed by signal (e.g. SIGINT / SIGTERM / SIGKILL) → -SIGNUM (POSIX shell convention)
        - still alive after the full ladder → None (should not happen with SIGKILL)
        """
        if not self._proc.isalive():
            return self._resolve_exit_code()
        try:
            if graceful:
                self._send_signal(signal.SIGINT)
                if self._wait_exit(grace_sec):
                    return self._resolve_exit_code()
                self._send_signal(signal.SIGTERM)
                if self._wait_exit(grace_sec):
                    return self._resolve_exit_code()
            self._send_signal(signal.SIGKILL)
            self._wait_exit(grace_sec)
        finally:
            try:
                self._proc.close(force=True)
            except Exception:
                pass
        return self._resolve_exit_code()

    def _resolve_exit_code(self) -> int | None:
        """ptyprocess sets `exitstatus` for normal exit, `signalstatus` for signal kill.
        Surface them as a single Optional[int] following the POSIX shell `-SIGNUM` convention."""
        es = self._proc.exitstatus
        if es is not None:
            return es
        ss = getattr(self._proc, "signalstatus", None)
        if ss is not None:
            return -ss
        return None

    def _send_signal(self, sig: int) -> None:
        try:
            os.kill(self._proc.pid, sig)
        except (OSError, ProcessLookupError):
            pass

    def _wait_exit(self, timeout_sec: float) -> bool:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            if not self._proc.isalive():
                return True
            time.sleep(0.05)
        return False

    @property
    def exit_code(self) -> int | None:
        if self._proc.isalive():
            return None
        return self._resolve_exit_code()


def spawn(
    argv: list[str],
    cwd: str | Path,
    env: dict[str, str] | None = None,
    rows: int = 30,
    cols: int = 120,
) -> PtyHandle:
    """Fork `argv` under a fresh PTY. Returns a PtyHandle."""
    cwd_str = str(cwd)
    final_env = dict(os.environ) if env is None else dict(env)
    proc = PtyProcess.spawn(
        argv,
        cwd=cwd_str,
        env=final_env,
        dimensions=(rows, cols),
    )
    # Put the master fd in non-blocking mode so the SessionManager reader
    # loop can safely call `os.read` from within a coroutine (via
    # `loop.add_reader`) without stalling the event loop when the child
    # produces output slowly. See `PtyHandle.write` for the matching
    # BlockingIOError handling on the write path.
    try:
        os.set_blocking(proc.fileno(), False)
    except Exception:
        pass
    return PtyHandle(proc)
