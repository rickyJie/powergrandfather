# Codex hooks — verified reference (codex-cli 0.145.0)

**Status:** confirmed by live capture, 2026-08-25. Everything below was
observed, not inferred. The previous revision of this doc was a "what we
know / TODO" sketch and several of its guesses were wrong; the corrections
are marked ⚠ so nobody re-derives them.

CSM does **not** wire codex hooks today — see
`backend/csm/modules/session_manager/codex_hooks.py` for why that's a
decision rather than an omission. This doc exists so the work is a
half-day if that decision is revisited, instead of another probing round.

## The one thing to read if you read nothing else

> **The rollout records NOTHING while a codex session is blocked on a
> prompt.** A reproduced approval block sat for 107.4 seconds between
> `custom_tool_call` (03:34:12) and `custom_tool_call_output`
> (03:36:00, `"aborted by user after 107.4s"`) with zero records in
> between. It is not that the record has an unexpected name — there is no
> record. **Any plan to detect a blocked codex session by parsing the
> rollout is dead on arrival.** Hooks are the only source.

## Event vocabulary

Read out of the binary's event enum and confirmed by firing them:

```
PreToolUse  PermissionRequest  PostToolUse  PreCompact  PostCompact
SessionStart  SessionEnd  UserPromptSubmit  SubagentStart  SubagentStop
```

⚠ The published docs also list `Stop`, but **0.145.0 does not have it**.
So hooks cannot replace the rollout's `task_complete` as the end-of-turn
signal.

⚠ Earlier guesses `on_notification` / `on_tool_use` / `on_task_complete`
do not exist. Names are CamelCase in the hooks config, not snake_case.

## Config shape

⚠ The old doc said `[hooks.<event>]` with a bare `command =`. That parses
without complaint and then **silently never fires** — the table nests two
levels:

```toml
[[hooks.PermissionRequest]]
matcher = ".*"
[[hooks.PermissionRequest.hooks]]
type = "command"
command = "/path/to/handler"
timeout = 20
```

`--strict-config` is useless as an oracle here: it rejects `hooks = 42`
("expected struct HooksToml") but accepts a nonsense field name, a
nonsense *event* name, and every wrong nesting shape. The subtree is only
parsed at hook-engine init. Don't trust it to validate a hook config.

### Inline injection (the good part)

⚠ The old doc said codex has "no equivalent CLI flag" to claude's
`--settings`, and proposed writing a profile TOML into
`<cwd>/.csm-codex-profiles/`. Both wrong, and the profile approach would
have littered every workspace codex ran in.

The whole table can be passed per-invocation with `-c`, whose value is
parsed as TOML:

```bash
codex -c 'hooks.PermissionRequest=[{matcher=".*", hooks=[{type="command", command="/path/handler", timeout=20}]}]'
```

Verified: hooks fired and `~/.codex/config.toml` stayed hook-free.

## Trust — the trap that bites hardest

Without `--dangerously-bypass-hook-trust`, codex stops at startup on an
interactive prompt:

```
Hooks need review
1 hook is new or changed. Hooks can run outside the sandbox after you trust them.
› 1. Review hooks   2. Trust…
```

and **never creates a session**. Getting this wrong doesn't degrade a
feature — it hangs every codex spawn.

The cost of passing it: it also auto-trusts hooks the *user* later adds to
their own `~/.codex/config.toml`. That trade is the main reason CSM hasn't
wired hooks.

## Payload

JSON on stdin, near-identical to claude's — `api/hooks.py` could consume it
almost verbatim. Note codex normalises its shell tool to **claude's** name.

```json
// SessionStart
{"session_id": "01a036ff-…", "transcript_path": "…/rollout-….jsonl",
 "cwd": "/tmp/work", "hook_event_name": "SessionStart",
 "model": "gpt-5.6-sol", "permission_mode": "default", "source": "startup"}

// UserPromptSubmit  — adds turn_id + prompt
{"…", "turn_id": "01a036ff-…", "prompt": "Run the shell command …"}

// PreToolUse
{"…", "turn_id": "…", "tool_name": "Bash",
 "tool_input": {"command": "id -un"},
 "tool_use_id": "exec-099d5c5d-…"}

// PermissionRequest  — same as PreToolUse minus tool_use_id
{"…", "turn_id": "…", "tool_name": "Bash", "tool_input": {"command": "id -un"}}

// SessionEnd
{"session_id": "…", "transcript_path": "…", "cwd": "…",
 "hook_event_name": "SessionEnd", "reason": "other"}
```

`transcript_path` does match claude's convention and points at the rollout.

A `PermissionRequest` hook may allow/deny via
`{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
"decision": {"behavior": "allow"|"deny", "message": "…"}}}`, or exit 2 with
a reason on stderr. Returning nothing (exit 0, no stdout) declines to decide
and lets codex's own prompt proceed — which is what CSM would want, since it
only needs to *observe*.

## Reproducing

Hooks only fire once a session actually exists, and **a codex TUI with no
prompt never creates one** — a no-prompt run produces no rollout and no
`SessionStart`. Any probe must submit a turn, or it proves nothing.

`codex` needs a real PTY; drive it with `pty.fork()` + `TIOCSWINSZ` (a 0×0
window makes the TUI render one character per line). Isolate with
`CODEX_HOME=<tmp>` and symlink `auth.json` in — do not copy the credential
into `/tmp`.

To force an approval block: `-s read-only -a untrusted` plus a prompt that
runs a non-trusted command.

## Related findings

- `--dangerously-bypass-approvals-and-sandbox` and `-a/--ask-for-approval`
  are **mutually exclusive in clap** — codex refuses to start. So a user
  who types an approval flag into CSM's Command field gets a loud failure,
  not a silent hang.
- MCP elicitation is **not** a stall path under bypass: a server that parks
  on `elicitation/create` gets an automatic `{"action":"decline"}`, the
  message never reaches the screen, and the rollout keeps ticking. (Tested
  with a purpose-built stdio MCP server.)

## Baseline integrity note

Running `codex doctor` WITHOUT `CODEX_HOME` set causes SQLite to create
`memories_1.sqlite-shm` / `-wal` in the real `~/.codex/`. Transient WAL side
files, no corruption. `scripts/csm-codex-dev-sandbox.sh` exports
`CODEX_HOME=$SANDBOX_ROOT/fake_codex` and wraps `codex` in a shell function
that refuses to run if `CODEX_HOME` is unset.
