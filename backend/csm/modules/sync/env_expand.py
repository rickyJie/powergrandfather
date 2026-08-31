"""B5 · `${VAR}` env-ref expansion for MCP entries.

MCP server templates often carry secrets (`SLACK_TOKEN=${SLACK_TOKEN}`).
Rule (§5 of spec):

- **Only** goes to the subprocess env dict — never argv (leaks via
  `/proc/<pid>/cmdline`, shell history, audit logs).
- Undefined var → `SyncPreflightError` listing the NAMES (not values,
  not "shape of value"). Sync is aborted before the CLI is invoked.
- Values with no `${...}` pass through untouched.
"""
from __future__ import annotations

import os
import re

from csm.modules.sync.errors import SyncPreflightError

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_env_refs(template_values: dict[str, str]) -> dict[str, str]:
    """Resolve `${VAR}` refs in each value against `os.environ`.

    Returns a NEW dict with the same keys; caller should splat this into
    the child process environment (`env={**os.environ, **resolved}`).
    """
    missing: set[str] = set()
    resolved: dict[str, str] = {}
    for k, raw in template_values.items():
        def _repl(m: re.Match[str]) -> str:
            var = m.group(1)
            val = os.environ.get(var)
            if val is None:
                missing.add(var)
                return ""
            return val
        resolved[k] = _VAR_RE.sub(_repl, raw)
    if missing:
        raise SyncPreflightError(
            f"undefined env vars: {sorted(missing)}",
            missing=sorted(missing),
        )
    return resolved


__all__ = ["resolve_env_refs"]
