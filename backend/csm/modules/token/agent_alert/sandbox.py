"""Subprocess sandbox for agent-generated check scripts.

The script is a Python source string containing `def check(window: dict) -> tuple[bool, dict]`.
The runner:
  1. Base64-encodes the window snapshot and passes it as `sys.argv[1]` to a
     small shim executed via `python -c`. The check script itself flows in
     via stdin as raw bytes.
  2. Runs with a 10s hard timeout.
  3. Returns a `SandboxResult` with either fired/payload or an error message.

Why base64(window) in argv + script via stdin (instead of a single stdin blob
with a delimiter):
  - Scripts can contain arbitrary bytes including NUL / newlines. A stdin
    delimiter would be corrupted the moment an agent emits one such byte.
  - Window JSON is small (< 4 KB in practice), well below argv max (~256 KB).
  - Both channels are byte-clean end-to-end: subprocess argv doesn't reinterpret
    bytes, and stdin is raw for the script.

No filesystem, no network — pure argv+stdin in, stdout JSON out. If the script
tries to `import os` and shells out anyway, that's on the trust model (single-
user local tool). Subprocess isolation buys us protection against accidental
damage (bad SQL, thread leaks) not adversarial code.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

# 10s is generous — check scripts should be pure arithmetic on a small dict.
DEFAULT_TIMEOUT_SEC = 10.0

# Runner shim executed as `python -c "<shim>" <base64_window>`.
# Reads the check script from stdin (raw bytes → utf-8), decodes window from
# argv[1] (base64 → utf-8 JSON), then execs the script and calls `check()`.
_SHIM = r'''
import base64, json, sys, traceback
if len(sys.argv) < 2:
    print(json.dumps({"error": "sandbox: missing window arg"}))
    sys.exit(0)
try:
    window = json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))
except Exception as e:
    print(json.dumps({"error": f"sandbox: window arg decode failed: {e}"}))
    sys.exit(0)
script = sys.stdin.read()
ns = {}
try:
    exec(compile(script, "<check_script>", "exec"), ns)
except Exception as e:
    print(json.dumps({"error": f"script compile error: {e}\n" + traceback.format_exc()}))
    sys.exit(0)
fn = ns.get("check")
if not callable(fn):
    print(json.dumps({"error": "script did not define a callable `check(window)`"}))
    sys.exit(0)
try:
    result = fn(window)
except Exception as e:
    print(json.dumps({"error": f"check() raised: {e}\n" + traceback.format_exc()}))
    sys.exit(0)
if not (isinstance(result, tuple) and len(result) == 2):
    print(json.dumps({"error": f"check() must return (bool, dict), got {type(result).__name__}"}))
    sys.exit(0)
fired, payload = result
if not isinstance(fired, bool):
    print(json.dumps({"error": f"check() first element must be bool, got {type(fired).__name__}"}))
    sys.exit(0)
if not isinstance(payload, dict):
    print(json.dumps({"error": f"check() second element must be dict, got {type(payload).__name__}"}))
    sys.exit(0)
try:
    out = json.dumps({"fired": fired, "payload": payload}, allow_nan=False)
except Exception as e:
    print(json.dumps({"error": f"payload not JSON-serializable: {e}"}))
    sys.exit(0)
print(out)
'''


@dataclass
class SandboxResult:
    """Outcome of one script execution."""
    fired: bool = False
    payload: dict | None = None
    error: str | None = None      # human-readable failure; None on success
    duration_sec: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


async def run_check_script(
    *,
    script: str,
    window: dict,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> SandboxResult:
    """Execute `script` against `window` in an isolated python subprocess.

    Never raises — all failures are captured into `SandboxResult.error` so the
    caller (evaluator / dry-run preview) can persist the error and keep going.
    """
    import time
    t0 = time.monotonic()

    # Serialize window BEFORE spawning — reject NaN/Inf early with a proper
    # error result instead of letting json.dumps raise or (worse) letting a
    # NaN slip past the guard and cascade through downstream comparisons.
    try:
        window_json = json.dumps(window, allow_nan=False)
    except ValueError as e:
        return SandboxResult(
            error=f"sandbox: window contains non-JSON-safe value (NaN/Inf?): {e}",
            duration_sec=time.monotonic() - t0,
        )
    except TypeError as e:
        return SandboxResult(
            error=f"sandbox: window contains non-serializable value: {e}",
            duration_sec=time.monotonic() - t0,
        )
    window_b64 = base64.b64encode(window_json.encode("utf-8")).decode("ascii")

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _SHIM, window_b64,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        return SandboxResult(error=f"sandbox: cannot spawn python: {e}",
                             duration_sec=time.monotonic() - t0)

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(input=script.encode("utf-8")),
            timeout=timeout_sec,
        )
    except TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        return SandboxResult(error=f"sandbox: script timed out after {timeout_sec}s",
                             duration_sec=time.monotonic() - t0)

    duration = time.monotonic() - t0

    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        stderr_tail = stderr_bytes.decode("utf-8", errors="replace")[-1024:]
        return SandboxResult(
            error=f"sandbox: python exited {proc.returncode}; stderr: {stderr_tail!r}",
            duration_sec=duration,
        )
    if not stdout:
        return SandboxResult(error="sandbox: no stdout", duration_sec=duration)

    # Last line is the payload (older Python interpreters may emit deprecation
    # noise before the payload; take the last non-empty line).
    last_line = ""
    for line in reversed(stdout.splitlines()):
        s = line.strip()
        if s:
            last_line = s
            break
    try:
        parsed = json.loads(last_line)
    except json.JSONDecodeError as e:
        return SandboxResult(
            error=f"sandbox: stdout not JSON ({e}); tail: {stdout[-512:]!r}",
            duration_sec=duration,
        )
    if not isinstance(parsed, dict):
        return SandboxResult(error=f"sandbox: stdout is {type(parsed).__name__}, expected dict",
                             duration_sec=duration)
    if "error" in parsed:
        return SandboxResult(error=str(parsed["error"]), duration_sec=duration)
    return SandboxResult(
        fired=bool(parsed.get("fired", False)),
        payload=dict(parsed.get("payload") or {}),
        duration_sec=duration,
    )
