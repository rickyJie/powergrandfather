"""Centralised resolution of Claude / Codex home directories.

All code that reads or writes files under `~/.claude` or `~/.codex` MUST go
through these helpers. Sandbox / dev builds override the roots via
`CSM_CLAUDE_HOME` and `CSM_CODEX_HOME` so the real user config is never
touched.

Contract:
    - `claude_home()` -> Path to Claude root (default `~/.claude`).
    - `codex_home()`  -> Path to Codex root  (default `~/.codex`).
    - Derived helpers (`claude_projects_dir()`, `codex_sessions_dir()` ...)
      compose on top so callers don't repeat path arithmetic.

Env overrides:
    CSM_CLAUDE_HOME=/tmp/csm-codex-sandbox-<user>/fake_claude
    CSM_CODEX_HOME=/tmp/csm-codex-sandbox-<user>/fake_codex

Sandbox mode (CSM_SANDBOX_MODE=1) is a *marker* consumed by main.py's
lifespan guard — this module doesn't enforce it, it only resolves paths.
"""

from __future__ import annotations

import os
from pathlib import Path


def _resolve(env_var: str, default_suffix: str) -> Path:
    # `if override:` alone would treat `""` as unset — but an empty env var
    # is a common misconfig (`export CSM_CLAUDE_HOME=` in a shell, or an
    # empty value in a `.env` file). Falling back to `~/.claude` in that
    # case would silently punch through the sandbox guard.
    override = os.environ.get(env_var)
    if override and override.strip():
        return Path(override).expanduser()
    return Path.home() / default_suffix


def claude_home() -> Path:
    """Root of Claude Code config / artifacts (default `~/.claude`)."""
    return _resolve("CSM_CLAUDE_HOME", ".claude")


def codex_home() -> Path:
    """Root of Codex CLI config / artifacts (default `~/.codex`)."""
    return _resolve("CSM_CODEX_HOME", ".codex")


def claude_projects_dir() -> Path:
    """`<claude_home>/projects` — JSONL transcripts live here."""
    return claude_home() / "projects"


def claude_settings_file() -> Path:
    """`<claude_home>/settings.json`."""
    return claude_home() / "settings.json"


def codex_sessions_dir() -> Path:
    """`<codex_home>/sessions` — Codex rollout files land under
    `YYYY/MM/DD/rollout-*.jsonl`."""
    return codex_home() / "sessions"


def codex_config_file() -> Path:
    """`<codex_home>/config.toml`."""
    return codex_home() / "config.toml"


def is_sandbox_mode() -> bool:
    """True when CSM_SANDBOX_MODE=1. Consumers may use this to relax /
    tighten behavior (e.g. main.py refuses to boot on the codex branch
    without it)."""
    return os.environ.get("CSM_SANDBOX_MODE") == "1"
