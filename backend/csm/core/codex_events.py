"""Codex rollout → CSM Event derivation.

Standalone functions kept OUT of `event_stream.py` for now because that
module's `_handle_record` is deeply Claude-shaped (per-session `_session_meta`
dict keyed by `external_session_id`, `_tailer` singleton, etc.). Rewiring it
into a proper multi-tailer / multi-derivation loop is a larger change than
fits the P4 slot on the codex branch and would risk regressing the working
Claude flow.

This module provides the pure translation layer. A follow-up (post-P7) can
plug it into EventStream by:
    - adding a second `CodexRolloutTailer` alongside `_tailer`
    - iterating both tailers in `_tick_once`
    - dispatching per-record to either `_handle_record` (Claude) or
      `_handle_codex_record` (Codex) — the latter calling into
      `derive_codex_events` here

Mapping reference: docs/codex/rollout_schema.md
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from csm.adapters.jsonl_tail import CodexRawRecord
from csm.core.events import Event, EventType

# Codex tool calls that carry no user-meaningful "what is it doing right now"
# signal. `wait` / `wait_agent` are codex polling a command it already started,
# so letting them win would replace the real tool name ("Bash: pytest …") with
# a meaningless "wait" for the rest of the turn — strictly worse than leaving
# the previous value in place.
_CODEX_INTERNAL_TOOLS = frozenset({"wait", "wait_agent"})

# Codex's shell family, reported as `Bash` so a codex session card reads the
# same as a claude one. This is codex's OWN normalisation, not ours: its
# PreToolUse hook payload reports `tool_name: "Bash"` for exactly these calls.
_CODEX_SHELL_TOOLS = frozenset({"exec_command", "shell", "local_shell"})

# Argument keys that are safe to show. Deliberately excludes `message`:
# `send_message` / `spawn_agent` / `followup_task` put a multi-KB encrypted
# blob there, and a truncated blob is worse than no hint at all.
_CODEX_HINT_KEYS = (
    "cmd", "command", "file_path", "path", "url", "pattern",
    "query", "target", "task_name",
    # Last resort: the raw code-mode script, when no literal argument could
    # be lifted out of it. Ugly, but "Bash: const cmds = […]" tells the user
    # more than a bare "Bash".
    "script",
)

# First `tools.<name>(` inside a code-mode `exec` script. Codex wraps every
# real call in a JS snippet, so the outer tool is always literally "exec" and
# the interesting name only exists inside the source.
_CODE_MODE_CALL_RE = re.compile(r"\btools\.([A-Za-z_][A-Za-z0-9_]*)\s*\(")
# `"cmd": "…"` / `cmd: '…'` inside that snippet. Non-greedy, escape-aware.
_CODE_MODE_CMD_RE = re.compile(
    r"""['"]?cmd['"]?\s*:\s*(['"])((?:\\.|(?!\1).)*)\1"""
)
# `q: "…"` entries inside a `tools.web__run({search_query:[{q:"…"}]})` script.
_CODE_MODE_QUERY_RE = re.compile(
    r"""['"]?q['"]?\s*:\s*(['"])((?:\\.|(?!\1).)*)\1"""
)
# Paths out of an apply_patch body — `*** Add File: /x/y.py` and friends.
# MUST run on JS-unescaped source: inside the script the patch is a string
# literal, so its line breaks are the two characters `\` + `n`, which `\S+`
# happily swallows along with the diff that follows (532 of 534 real paths
# came out as `/tmp/a.md\n+---\n+schema:` before this was decoded first).
_PATCH_FILE_RE = re.compile(
    r"\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(\S+)"
)
# Per-argument display cap. The chat card shows arguments verbatim, so one
# heredoc must not swamp the frame; `_truncate_input` caps again downstream.
_ARG_VALUE_CAP = 400


# JS string escapes we may see when a hint is lifted straight out of code-mode
# source rather than out of parsed JSON. Without decoding these, a multi-line
# shell command renders on the card as a literal `foo\nbar`.
_JS_ESCAPES = (("\\\\", "\\"), ("\\n", " "), ("\\t", " "), ("\\r", " "),
               ('\\"', '"'), ("\\'", "'"))


def _clean_hint(value: str, *, unescape: bool = False) -> str:
    """Collapse a raw argument value into one card-safe display line.

    `unescape` is for values captured from JavaScript source, where escapes
    are still literal two-character sequences. Values that came through
    `json.loads` are already decoded and must NOT be unescaped again.
    """
    if unescape:
        for src, dst in _JS_ESCAPES:
            value = value.replace(src, dst)
    # Any real newline/tab would break the single-line card layout too.
    return " ".join(value.split())


def _first_hint(args: Any) -> str:
    """Pick the first human-meaningful value out of a tool's arguments."""
    if not isinstance(args, dict):
        return ""
    for key in _CODEX_HINT_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            return _clean_hint(val)
    return ""


def _sanitise_args(args: Any) -> dict[str, Any]:
    """Card-safe copy of a tool's arguments.

    Drops `message` outright — `send_message` / `spawn_agent` /
    `followup_task` put a multi-KB encrypted blob there, and rendering a
    truncated blob in a chat bubble is worse than rendering nothing — and caps
    every string so one heredoc argument can't dominate the transcript frame.
    """
    if not isinstance(args, dict):
        return {}
    out: dict[str, Any] = {}
    for key, val in args.items():
        if key == "message":
            continue
        out[key] = _clean_hint(val)[:_ARG_VALUE_CAP] if isinstance(val, str) else val
    return out


def codex_tool_call(payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Map a codex tool-call record to ``(display_name, args)``.

    ONE parser, shared by the two consumers that need it: `current_tool` on
    the session card (via `_codex_tool_progress`) and the tool cards in the
    chat stream (`modules/agent/message_router.py`). They previously would
    have needed the same code-mode unwrapping, and two copies of it would
    have drifted.

    Returns ``None`` for records that should produce nothing at all (internal
    polling, or a nameless call). Codex has two call shapes:

      * ``function_call``    — ``arguments`` is a JSON string.
      * ``custom_tool_call`` — ``name`` is always ``exec`` and ``input`` is a
        JavaScript snippet ("code mode"); the real tool is inside the source.

    The shell family collapses to ``Bash`` with a ``command`` argument, which
    is exactly claude's `tool_input` shape — so a codex tool card renders
    through the identical frontend path as a claude one.
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        return None

    if payload.get("type") == "custom_tool_call" or payload.get("input"):
        src = str(payload.get("input") or "")
        inner = _CODE_MODE_CALL_RE.search(src)
        # A code-mode script with no recognisable tools.* call still means
        # "codex is executing something" — keep the outer name rather than
        # dropping the signal entirely.
        name = inner.group(1) if inner else name
        if name in _CODEX_INTERNAL_TOOLS:
            return None
        args: dict[str, Any] = {}
        if name in _CODEX_SHELL_TOOLS:
            cmd = _CODE_MODE_CMD_RE.search(src)
            command = _clean_hint(cmd.group(2), unescape=True) if cmd else ""
            if command:
                args = {"command": command[:_ARG_VALUE_CAP]}
            name = "Bash"
        elif name == "apply_patch":
            # The changed paths are only in the patch body at call time;
            # codex's structured `patch_apply_end` doesn't arrive until the
            # write finishes. Reading them here is what lets the card say
            # WHICH files before the edit lands.
            files = _PATCH_FILE_RE.findall(_clean_hint(src, unescape=True))
            if files:
                args = {"files": files}
        elif name == "web__run":
            queries = _CODE_MODE_QUERY_RE.findall(src)
            if queries:
                args = {"query": ", ".join(q[1] for q in queries)[:_ARG_VALUE_CAP]}
        if not args and src:
            # No literal argument to lift: codex frequently builds the command
            # list in JS and loops (`const cmds = [[label, cmd], …]`), so there
            # is no `cmd: "…"` to find. Falling back to the script itself keeps
            # the card honest about what is running — an empty card is the one
            # outcome that helps nobody, and "what is it doing right now" is
            # the entire reason these cards exist.
            args = {"script": _clean_hint(src)[:_ARG_VALUE_CAP]}
        return name, args

    if name in _CODEX_INTERNAL_TOOLS:
        return None
    raw: Any = payload.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = None
    args = _sanitise_args(raw)
    if name in _CODEX_SHELL_TOOLS:
        command = args.pop("cmd", None) or args.pop("command", None) or ""
        return "Bash", ({"command": command, **args} if command else args)
    return name, args


def _codex_tool_progress(payload: dict[str, Any]) -> tuple[str, str] | None:
    """`(tool_name, one-line hint)` for the session card's `current_tool`."""
    mapped = codex_tool_call(payload)
    if mapped is None:
        return None
    name, args = mapped
    return name, _first_hint(args)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _non_negative_int(value: Any) -> int:
    """Best-effort token counter coercion for rollout payloads."""
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalise_input_usage(last: dict[str, Any]) -> tuple[int, int, int, int]:
    """Split Codex's inclusive input count into CSM's disjoint buckets.

    Codex reports ``input_tokens`` as the complete input count. Its
    ``cached_input_tokens`` and ``cache_write_input_tokens`` fields are
    details *inside* that total, whereas CSM's storage model expects
    ``input_tokens``, cache-read, and cache-creation to be disjoint so they
    can safely be summed by the Tokens API.

    Return ``(uncached_input, cache_read, cache_write, inclusive_input)``.
    Detail counters are capped to the inclusive total defensively so a
    partially rendered or future payload cannot create a negative uncached
    count or reintroduce double-counting.
    """
    inclusive_input = _non_negative_int(last.get("input_tokens"))
    cache_read = min(
        _non_negative_int(last.get("cached_input_tokens")),
        inclusive_input,
    )
    remaining = inclusive_input - cache_read
    cache_write = min(
        _non_negative_int(last.get("cache_write_input_tokens")),
        remaining,
    )
    return remaining - cache_write, cache_read, cache_write, inclusive_input


def derive_codex_events(record: CodexRawRecord) -> Iterable[Event]:
    """Yield zero or more CSM Events for a single codex rollout record.

    Return type is an iterable (generator) so callers can `yield from` or
    `list(...)`. Empty for records that don't map to a domain event
    (world_state, turn_context, response_item — the latter overlaps with
    event_msg::agent_message which we already surface).
    """
    obj = record.obj
    top_type = obj.get("type")
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = payload.get("type")
    ts = _parse_iso(obj.get("timestamp")) or datetime.now(UTC)

    # -- session_meta → SESSION_STARTED --
    if top_type == "session_meta":
        yield Event(
            type=EventType.SESSION_STARTED,
            ts=ts,
            session_id=record.codex_session_id,
            project_path=record.project_path,
            payload={
                "backend": "codex",
                "rollout_path": record.rollout_path,
                "cli_version": payload.get("cli_version"),
                "model_provider": payload.get("model_provider"),
            },
            source_offset=record.byte_offset,
        )
        return

    # -- response_item::{function_call,custom_tool_call} → SESSION_TOOL_PROGRESS --
    # Codex emits NO `event_msg` for an ordinary tool call, so without this the
    # session card shows "running" with an empty `current_tool` for most of a
    # turn (measured at 60% of RUNNING wall-clock across 30 real rollouts).
    # Claude gets the same signal from its PreToolUse hook; codex has no hooks
    # wired, so the rollout is the only place this exists.
    if top_type == "response_item" and payload_type in (
        "function_call", "custom_tool_call"
    ):
        mapped = _codex_tool_progress(payload)
        if mapped is None:
            return
        tool_name, hint = mapped
        yield Event(
            type=EventType.SESSION_TOOL_PROGRESS,
            ts=ts,
            session_id=record.codex_session_id,
            project_path=record.project_path,
            payload={
                "backend": "codex",
                "tool_name": tool_name,
                "tool_hint": hint,
                "call_id": payload.get("call_id"),
                "rollout_path": record.rollout_path,
            },
            source_offset=record.byte_offset,
        )
        return

    if top_type != "event_msg":
        # world_state / turn_context / other response_items → no domain event
        return

    # -- event_msg::user_message → MESSAGE_USER --
    if payload_type == "user_message":
        yield Event(
            type=EventType.MESSAGE_USER_SENT,
            ts=ts,
            session_id=record.codex_session_id,
            project_path=record.project_path,
            payload={"text": payload.get("message", ""), "backend": "codex"},
            source_offset=record.byte_offset,
        )
        return

    # -- event_msg::task_complete → MESSAGE_ASSISTANT_DONE --
    if payload_type == "task_complete":
        yield Event(
            type=EventType.MESSAGE_ASSISTANT_DONE,
            ts=ts,
            session_id=record.codex_session_id,
            project_path=record.project_path,
            payload={
                "assistant_text": payload.get("last_agent_message", ""),
                # Backward-compatible alias for existing event consumers.
                "text": payload.get("last_agent_message", ""),
                "backend": "codex",
                "rollout_path": record.rollout_path,
                "duration_ms": payload.get("duration_ms"),
                "turn_id": payload.get("turn_id"),
            },
            source_offset=record.byte_offset,
        )
        return

    # -- event_msg::turn_aborted → MESSAGE_ASSISTANT_DONE --
    # An aborted turn emits no task_complete, so without this the session sits
    # at RUNNING (and keeps a stale `current_tool`) until some later event or
    # process exit clears it. Claude gets the equivalent release from its Stop
    # hook; codex has no hooks, so the rollout is the only signal. A Ctrl-C
    # typed into the terminal is already caught by SessionManager's interrupt
    # sniffing, but an abort raised inside codex's own TUI never touches the
    # PTY write path — this is the only way we hear about those.
    if payload_type == "turn_aborted":
        yield Event(
            type=EventType.MESSAGE_ASSISTANT_DONE,
            ts=ts,
            session_id=record.codex_session_id,
            project_path=record.project_path,
            payload={
                # No assistant text: the turn produced no final message, and
                # inventing one would overwrite `last_assistant_msg` with a
                # status string in every session card.
                "backend": "codex",
                "aborted": True,
                "reason": payload.get("reason"),
                "rollout_path": record.rollout_path,
                "duration_ms": payload.get("duration_ms"),
                "turn_id": payload.get("turn_id"),
            },
            source_offset=record.byte_offset,
        )
        return

    # -- event_msg::token_count → USAGE_RECORDED --
    if payload_type == "token_count":
        info = payload.get("info", {}) if isinstance(payload.get("info"), dict) else {}
        last = info.get("last_token_usage", {}) if isinstance(info.get("last_token_usage"), dict) else {}
        input_tokens, cache_read_tokens, cache_write_tokens, inclusive_input = (
            _normalise_input_usage(last)
        )
        yield Event(
            type=EventType.USAGE_RECORDED,
            ts=ts,
            session_id=record.codex_session_id,
            project_path=record.project_path,
            payload={
                "backend": "codex",
                # Model — bootstrapped from session_meta on line 0; falls
                # back to None on ancient rollouts that don't set it. The
                # aggregator writes this into `raw_token_event.model` so
                # pricing / MODEL-scope budgets can distinguish codex spend
                # from claude.
                "model": record.model,
                # CSM token buckets are disjoint. Codex's raw input counter
                # includes both cache detail counters, so normalize at the
                # adapter boundary before shared aggregation/cost code sees it.
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read_tokens,
                "cache_creation_input_tokens": cache_write_tokens,
                "output_tokens": _non_negative_int(last.get("output_tokens")),
                # Codex-specific: keep raw payload for later pricing work.
                "_codex_input_tokens_inclusive": inclusive_input,
                "_codex_total_tokens": _non_negative_int(last.get("total_tokens")),
                "_codex_reasoning_output_tokens": _non_negative_int(
                    last.get("reasoning_output_tokens")
                ),
                "_codex_rate_limits": payload.get("rate_limits"),
            },
            source_offset=record.byte_offset,
        )
        return

    # -- event_msg::patch_apply_end → SESSION_TOOL_PROGRESS --
    # New Codex versions wrap apply_patch in a generic exec call. This
    # structured completion event is the only reliable live signal that files
    # changed, and drives the Sessions "Changes" panel refresh.
    if payload_type == "patch_apply_end" and payload.get("success") is True:
        changes = payload.get("changes")
        changed_files = (
            [str(path) for path in changes if isinstance(path, str)]
            if isinstance(changes, dict) else []
        )
        yield Event(
            type=EventType.SESSION_TOOL_PROGRESS,
            ts=ts,
            session_id=record.codex_session_id,
            project_path=record.project_path,
            payload={
                "backend": "codex",
                "tool_name": "apply_patch",
                "tool_hint": changed_files[0] if changed_files else "",
                "changed_files": changed_files,
                "file_path": changed_files[0] if changed_files else None,
                "rollout_path": record.rollout_path,
            },
            source_offset=record.byte_offset,
        )
        return

    # task_started / agent_message (non-final) / turn_aborted etc:
    # not surfaced as CSM events in this pass. Extending is straightforward.
    return


def summarise_codex_events(records: Iterable[CodexRawRecord]) -> dict[str, Any]:
    """Convenience utility for tests / diagnostics: count derived events
    by type across a stream of raw records."""
    from collections import Counter
    types: Counter[str] = Counter()
    for r in records:
        for ev in derive_codex_events(r):
            types[str(ev.type.value)] += 1
    return dict(types)
