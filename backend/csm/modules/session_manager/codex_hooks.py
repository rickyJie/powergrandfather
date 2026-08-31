"""Codex hook configuration — verified reference, deliberately NOT wired.

Everything below was established by live probing codex-cli 0.145.0 in an
isolated `CODEX_HOME` on 2026-08-25. The previous version of this module was
a stub built on guesses, and every one of those guesses turned out to be
wrong; the corrections are called out inline so nobody re-derives them.

## Why nothing here is called

CSM does not inject codex hooks today. That is a decision, not an omission:
wiring them requires passing `--dangerously-bypass-hook-trust` on every codex
spawn (see "Trust" below), which would ALSO silently auto-trust any hooks the
user later adds to their own `~/.codex/config.toml`. The two things hooks
would buy us are currently low-value:

  * `PermissionRequest` → `WAITING_AUTH`. Unreachable in practice: CSM always
    spawns with `--dangerously-bypass-approvals-and-sandbox`, so codex never
    asks. And a user who adds `-a untrusted` to the Command field doesn't get
    a silent hang — clap rejects the combination and codex refuses to start.
  * Killing the <=5s rollout poll lag on state transitions. A latency nicety,
    not a correctness bug.

`current_tool` — the one state field that WAS genuinely broken — needed no
hooks at all and is now derived from the rollout in `core/codex_events.py`.

## Config shape (corrected)

The old stub claimed hooks are `[hooks.<event>]` tables and that codex has
"no equivalent CLI flag" to claude's `--settings`. Both wrong. Hooks nest
TWO levels, and event names are CamelCase:

    [[hooks.PermissionRequest]]
    matcher = ".*"
    [[hooks.PermissionRequest.hooks]]
    type = "command"
    command = "/path/to/handler"
    timeout = 20

and the whole table can be passed inline per-invocation via `-c`, which IS
the exact analogue of claude's `--settings` and leaves the user's
`~/.codex/config.toml` untouched (verified: file stayed hook-free across a
run whose hooks fired). That makes the old plan — writing a profile TOML into
`<cwd>/.csm-codex-profiles/` — both unnecessary and user-hostile, since it
littered every workspace codex ran in.

## Trust (the trap)

Without `--dangerously-bypass-hook-trust`, codex stops at startup on an
interactive "Hooks need review / 1. Review hooks 2. Trust" prompt and never
creates a session. Getting this wrong doesn't degrade a feature — it hangs
every codex session at spawn.

## Payload

Delivered as JSON on stdin, near-identical to claude's, so `api/hooks.py`
could consume it almost verbatim. Codex even normalises its shell tool to
claude's name:

    {"session_id", "transcript_path", "cwd", "hook_event_name", "model",
     "permission_mode",
     # SessionStart:      "source"                (startup | ...)
     # UserPromptSubmit:  "prompt"
     # PreToolUse:        "tool_name" ("Bash"), "tool_input", "tool_use_id"
     # PermissionRequest: "tool_name", "tool_input"
     # SessionEnd:        "reason"
     ...}

See `docs/codex/hooks_payload.md` for full captures and the probe method.
"""

from __future__ import annotations

from pathlib import Path

# The real vocabulary, read out of the 0.145.0 binary's event enum and
# confirmed by firing them. NOTE: the published docs also list `Stop`, but
# this build does NOT have it — so hooks cannot replace the rollout's
# `task_complete` as the end-of-turn signal.
HOOK_EVENT_NAMES: tuple[str, ...] = (
    "SessionStart",
    "SessionEnd",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
)

# Mandatory alongside any injected hook config — see "Trust" above.
HOOK_TRUST_BYPASS_FLAG = "--dangerously-bypass-hook-trust"


def write_codex_hooks_config(
    cwd_sandbox: Path,
    sid: str,
    hooks_base_url: str,
) -> Path:
    """Removed. Kept as a loud tombstone rather than a silent no-op.

    The old implementation created `<cwd>/.csm-codex-profiles/` inside
    whatever repo codex was running in and wrote a stub file there that did
    not enable anything. Hooks are injected via `-c` inline TOML instead —
    no files, no per-workspace residue.
    """
    raise NotImplementedError(
        "codex hooks are injected via inline `-c` TOML, not a profile file; "
        "and CSM does not wire them today. See this module's docstring."
    )
