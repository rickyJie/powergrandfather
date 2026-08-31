"""Claude hooks-config generator.

Emits the JSON blob passed to `claude --settings <json>` so every hook
event POSTs back to CSM. Extracted from `csm.modules.session_manager.manager`
so ClaudeAdapter can own it; M3 will drop the copy in manager.py.

The hook uses `type: command` (not `type: http`) because Claude Code
2.1.112+ enforces HTTPS on native http hooks, and CSM's uvicorn is plain
http on 127.0.0.1. The command shells out to `python3 -c '...'` which
reads stdin JSON and POSTs it to CSM — no TLS constraint, no HTTP_PROXY
interception (loopback direct from the shell process).
"""
from __future__ import annotations

import json
import shlex

_HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "Notification",
    "Stop",
    "SessionEnd",
)


def build_hook_py_code(hook_url: str) -> str:
    """Emit the `python3 -c` snippet each claude hook shells out to.

    HTTPS branch attaches an unverified SSL context (CSM's cert is
    self-signed; only caller is loopback CSM so MITM is not a threat).
    Plain-http branch stays byte-identical to the pre-TLS behaviour.
    """
    if hook_url.startswith("https://"):
        return (
            "import sys, ssl, urllib.request as u\n"
            "d = sys.stdin.buffer.read()\n"
            "try:\n"
            f"    u.urlopen(u.Request({hook_url!r}, data=d, "
            "headers={'Content-Type':'application/json'}), timeout=3, "
            "context=ssl._create_unverified_context()).read()\n"
            "except Exception: pass\n"
        )
    return (
        "import sys, urllib.request as u\n"
        "d = sys.stdin.buffer.read()\n"
        "try:\n"
        f"    u.urlopen(u.Request({hook_url!r}, data=d, "
        "headers={'Content-Type':'application/json'}), timeout=3).read()\n"
        "except Exception: pass\n"
    )


def build_hooks_settings_json(hook_url: str) -> str:
    """Return the JSON string ready to hand to `claude --settings <json>`.

    Every event listed in `_HOOK_EVENTS` fires the same command hook that
    POSTs its stdin to `hook_url`. Timeouts default to 5s (short enough
    that a stalled CSM doesn't wedge the CLI, long enough to survive a
    slow-first-request cold start).
    """
    py_code = build_hook_py_code(hook_url)
    hook_cmd = f"python3 -c {shlex.quote(py_code)}"
    settings = {
        "hooks": {
            ev: [{
                "matcher": "",
                "hooks": [{"type": "command", "command": hook_cmd, "timeout": 5}],
            }]
            for ev in _HOOK_EVENTS
        }
    }
    return json.dumps(settings, separators=(",", ":"))


__all__ = ["build_hook_py_code", "build_hooks_settings_json"]
