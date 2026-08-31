"""Proxy environment resolution for spawned Claude/Codex sessions.

Problem
-------
CSM is typically started by `scripts/start.sh` (bash) or under a service
manager. Neither of those source the user's `~/.zshrc`, so proxy variables
that only live in an interactive zsh RC file (`HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY`, `NO_PROXY` and their lowercase aliases) never reach uvicorn's
`os.environ`, and consequently never reach the `claude` / `codex`
subprocesses spawned by `SessionManager`. Users then see network failures
against `api.anthropic.com` / `chatgpt.com` and think the app is broken.

Resolution strategy (see docs/backends/... — proxy handling)
------------------------------------------------------------
Two independent sources, layered in a fixed order:

    sniff_from_login_shell()   ← "$SHELL -ic 'export -p'" whitelist parse
              │
              ▼
    read_env_file()            ← ~/.csm/proxy.env plain KEY=VALUE
              │
              ▼
    ProxyResolveResult.env     ← file overrides sniff

The caller (`SessionManager.__init__`) invokes `resolve_proxy_env()` once
per process, caches the resulting dict, and layers it under any explicit
per-request `env` and above `os.environ` at spawn time. This module never
mutates `os.environ`.

Safety
------
- `sniff_from_login_shell` runs `$SHELL -ic 'export -p'` with a bounded
  timeout (`proxy_sniff_timeout_sec`). Stderr is discarded so a chatty
  rc file (colored MOTD, tool banners) doesn't pollute CSM's logs.
- Only whitelisted variable names are ever returned; anything the user's
  rc file exports outside the whitelist is silently dropped. This keeps
  the blast radius minimal — we don't accidentally inherit `PATH` / `PS1`
  / `AWS_SECRET_ACCESS_KEY` / etc.
- All failures (missing shell, timeout, non-zero exit, parse error) are
  logged and returned as an empty dict + a warning string; they never
  raise.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


PROXY_WHITELIST: frozenset[str] = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)


@dataclass(frozen=True)
class ProxyResolveResult:
    """Merged proxy env + per-variable source metadata + warnings."""

    env: dict[str, str] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)  # var → "sniff" | "file"
    sniff_shell: str | None = None
    env_file_path: Path | None = None
    env_file_exists: bool = False
    warnings: tuple[str, ...] = ()


def sniff_from_login_shell(
    shell: str,
    *,
    timeout: float = 3.0,
) -> tuple[dict[str, str], list[str]]:
    """Run `$SHELL -ic 'export -p'` and extract whitelisted proxy vars.

    Returns (env_dict, warnings). Never raises.

    `export -p` prints one `export NAME='VALUE'` line per exported variable
    in a shell-quoted form; using `shlex` to split each line handles quotes,
    escapes, and embedded spaces correctly.
    """
    warnings: list[str] = []
    if not shell:
        return {}, ["proxy sniff skipped: $SHELL is unset"]

    try:
        proc = subprocess.run(
            [shell, "-ic", "export -p"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {}, [f"proxy sniff skipped: shell not found: {shell}"]
    except subprocess.TimeoutExpired:
        return {}, [f"proxy sniff timed out after {timeout}s: {shell}"]
    except OSError as exc:
        return {}, [f"proxy sniff failed: {exc}"]

    if proc.returncode != 0:
        warnings.append(
            f"proxy sniff: {shell} exited with rc={proc.returncode}; parsing stdout anyway"
        )

    env: dict[str, str] = {}
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Accept both `export NAME=VALUE` (bash / zsh) and `NAME=VALUE`
        # (some shells / typeset -x variants).
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        elif line.startswith("declare -x "):
            line = line[len("declare -x ") :].lstrip()
        elif line.startswith("typeset -x "):
            line = line[len("typeset -x ") :].lstrip()
        if "=" not in line:
            continue
        name, _, rhs = line.partition("=")
        if name not in PROXY_WHITELIST:
            continue
        try:
            # rhs is shell-quoted; shlex handles single-quoted / double-quoted / bare.
            tokens = shlex.split(rhs, posix=True)
        except ValueError as exc:
            warnings.append(f"proxy sniff: could not parse value for {name}: {exc}")
            continue
        value = tokens[0] if tokens else ""
        env[name] = value

    return env, warnings


def read_env_file(path: Path) -> tuple[dict[str, str], list[str]]:
    """Parse a KEY=VALUE file, applying the same whitelist as sniff.

    Returns (env_dict, warnings). Never raises for I/O errors — missing
    file is a silent no-op (empty dict, empty warnings). Only malformed
    lines produce warnings.

    Format:
        # comments start with '#'
        HTTP_PROXY=http://proxy.example.com:7890
        HTTPS_PROXY="http://proxy.example.com:7890"
        NO_PROXY='localhost,127.0.0.1'

    Non-whitelisted keys are silently dropped.
    """
    warnings: list[str] = []
    if not path.exists():
        return {}, warnings
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {}, [f"proxy env file unreadable: {path}: {exc}"]

    env: dict[str, str] = {}
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            warnings.append(f"proxy env file {path}:{lineno}: malformed line (no '=')")
            continue
        name, _, rhs = line.partition("=")
        name = name.strip()
        if name not in PROXY_WHITELIST:
            continue
        rhs = rhs.strip()
        # Strip matching outer quotes.
        if len(rhs) >= 2 and rhs[0] == rhs[-1] and rhs[0] in {"'", '"'}:
            rhs = rhs[1:-1]
        env[name] = rhs
    return env, warnings


def resolve_proxy_env(
    *,
    auto_sniff: bool,
    env_file: Path | None,
    shell: str | None = None,
    sniff_timeout: float = 3.0,
) -> ProxyResolveResult:
    """Merge sniffed + file-based proxy env.

    Precedence: env_file overrides sniff (file is the explicit user
    override; if they wrote it down we trust it over whatever the RC file
    happens to export today).

    `shell` defaults to `$SHELL`; passing `""` skips sniff entirely.
    `auto_sniff=False` skips sniff regardless of `shell`.
    """
    if shell is None:
        shell = os.environ.get("SHELL", "")

    all_warnings: list[str] = []
    sniff_env: dict[str, str] = {}
    if auto_sniff and shell:
        sniff_env, w = sniff_from_login_shell(shell, timeout=sniff_timeout)
        all_warnings.extend(w)

    file_env: dict[str, str] = {}
    env_file_exists = False
    if env_file is not None:
        env_file_exists = env_file.exists()
        if env_file_exists:
            file_env, w = read_env_file(env_file)
            all_warnings.extend(w)

    merged: dict[str, str] = {}
    sources: dict[str, str] = {}
    for k, v in sniff_env.items():
        merged[k] = v
        sources[k] = "sniff"
    for k, v in file_env.items():
        merged[k] = v
        sources[k] = "file"

    return ProxyResolveResult(
        env=merged,
        sources=sources,
        sniff_shell=shell if auto_sniff else None,
        env_file_path=env_file,
        env_file_exists=env_file_exists,
        warnings=tuple(all_warnings),
    )
