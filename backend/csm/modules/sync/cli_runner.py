"""B3 · Subprocess wrapper for local CLI calls (sync subsystem).

Rules (locked in by spec §3):
- `asyncio.create_subprocess_exec` — **never** `shell=True`.
- Hard timeout (default 10s). On timeout the child is `kill()`ed and
  drained; caller sees `returncode is None` + `timed_out=True`.
- Judgment is **returncode-only**: 0 → ok, non-zero → error. `stderr`
  is captured for logging but MUST NOT be parsed for natural-language
  error strings (fragile across CLI versions + i18n).
- `env`, if provided, is passed verbatim as the child's environment.
  Caller is responsible for scoping (e.g. B5 splats `${VAR}` values
  into `{**os.environ, **resolved}` at call site).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CLIResult:
    """Outcome of a single local-CLI subprocess call.

    `returncode is None` iff the wrapper hit its timeout — the kill was
    already sent; caller should treat this as SyncStatus.TIMEOUT and
    NOT retry (drift poll worker handles the retry cadence).

    `stderr` is the raw string as reported by the CLI; per spec §3
    (B3), it is written verbatim to `sync_activity.detail_json` and
    NEVER parsed for natural-language error messages.
    """
    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool

    @property
    def ok(self) -> bool:
        """True iff the CLI exited with returncode == 0."""
        return self.returncode == 0


async def run_cli(
    argv: list[str],
    *,
    timeout: float = 10.0,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> CLIResult:
    """Run a local CLI subprocess with a hard timeout.

    Never invokes a shell (argv is passed as separate args). Captures
    stdout/stderr as bytes and decodes with `errors="replace"` so a
    corrupt CLI byte stream cannot crash the caller.

    On timeout: sends SIGKILL to the child, drains its pipes to avoid
    ResourceWarnings, and returns a CLIResult with `returncode=None,
    timed_out=True`. Caller treats this as SyncStatus.TIMEOUT.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout,
        )
    except TimeoutError:
        # Kill + drain. Second communicate() is fenced in try/except
        # because the child may already be gone.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.communicate()
        except Exception:
            pass
        return CLIResult(
            argv=tuple(argv),
            returncode=None,
            stdout="",
            stderr="",
            duration_ms=int((loop.time() - started) * 1000),
            timed_out=True,
        )

    return CLIResult(
        argv=tuple(argv),
        returncode=proc.returncode,
        stdout=stdout_b.decode("utf-8", errors="replace"),
        stderr=stderr_b.decode("utf-8", errors="replace"),
        duration_ms=int((loop.time() - started) * 1000),
        timed_out=False,
    )


async def probe_sync_capabilities(cli_name: str) -> frozenset[str]:
    """Return the set of `{"mcp", "skills"}` the CLI actually supports.

    Called once at adapter construction (or every drift-poll tick) — result
    is diffed against the adapter's declared `capabilities`. String keys
    (not the Capability enum) so this helper stays independent of the
    backends package; callers translate to `Capability.SYNC_MCP` /
    `SYNC_SKILLS` themselves.
    """
    supported: set[str] = set()
    r = await run_cli([cli_name, "mcp", "--help"], timeout=3.0)
    if r.ok:
        supported.add("mcp")
    r = await run_cli([cli_name, "skill", "--help"], timeout=3.0)
    if r.ok:
        supported.add("skills")
    return frozenset(supported)


__all__ = ["CLIResult", "run_cli", "probe_sync_capabilities"]
