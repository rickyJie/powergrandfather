"""Translate CLI transcript records into structured chat messages.

Multi-agent v2: supports both claude JSONL shape (message.role /
message.content blocks) and codex rollout shape (event_msg /
session_meta). Router auto-detects by the record's top-level `type`.

One record may carry multiple content blocks (e.g. an assistant message
with a text block followed by a tool_use block) — the router splits these
into one structured event per block, in the original order, so the
frontend can render them as discrete chat-bubble / tool-card elements.

Output event shapes (all dicts; `ts` is the record's timestamp string verbatim):
  {"type": "user_message",     "ts", "text"}
  {"type": "assistant_text",   "ts", "text"}
  {"type": "tool_use_start",   "ts", "tool", "input", "tool_id"}
  {"type": "tool_use_result",  "ts", "tool_id", "ok", "preview"}
  {"type": "system_note",      "ts", "text"}     # background / meta events
      # Optional "level": "warning" — set when the event it reports did not
      # succeed. Absent means routine, which is the overwhelming majority.

Unknown record shapes are silently ignored — the router must never crash on
unfamiliar payloads from upstream releases.
"""
from __future__ import annotations

import re
from typing import Any

from csm.core.codex_events import codex_tool_call
from csm.core.transcript_provenance import is_injected_user_record

# Claude Code injects control scaffolding into the JSONL as ordinary "user"
# message text: the DO-NOT-respond caveat around local-command output, and the
# <command-name>/<command-message>/<command-args> block for a slash command.
# Rendered raw in the chat these are pure noise. Strip the caveat, collapse a
# slash-command block to a concise "/name args" line, and unwrap stdout.
_CAVEAT_RE = re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.DOTALL)
_CMD_NAME_RE = re.compile(r"<command-name>\s*/?(.*?)\s*</command-name>", re.DOTALL)
_CMD_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)
_CMD_TAG_RE = re.compile(
    r"</?command-(?:name|message|args|contents)>", re.DOTALL
)
_STDOUT_RE = re.compile(
    r"</?local-command-stdout>", re.DOTALL
)
# VT/ANSI escape sequences leak into local-command-stdout (e.g. /compact writes
# "[2mCompacted (ctrl+o…)[22m"). The ESC byte renders invisibly, so
# the chat shows garbled "[2mCompacted…[22m". Strip them before display.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
# The /compact status line is pure UI chrome, not conversation content — a
# stdout-only record that reduces to this marker should produce no chat bubble.
_COMPACT_NOISE_RE = re.compile(r"^Compacted\b.*$", re.IGNORECASE)

# --------------------------------------------------------------------------
# "Did the human type this?"
#
# Claude Code files a lot of machine-generated text under role "user". It all
# belongs in the transcript, but surfaces that attribute text TO the user — the
# chat bubble's styling, the mobile jump rail — must not treat it as theirs.
# The judgement itself lives in `csm.core.transcript_provenance` because
# EventStream and the claude adapter make the same call when deciding whether a
# record means "the human spoke"; it must not drift between them.

# A `<task-notification>` — a subagent or a background command reporting back —
# also arrives as role-"user" text, wrapped in an XML envelope: task-id,
# tool-use-id, output-file, a boilerplate <note> identical on every one, and a
# <result> that reaches 12KB. Rendered verbatim it buries the conversation on a
# phone screen. <summary> is the one line worth showing and is present on all
# 390 in the local corpus; everything else is plumbing the user cannot act on.
#
# The envelope is recognised by `origin.kind` (all 390 carry it), not by
# matching the text — otherwise quoting a notification in a real message would
# replace what you wrote with a summary of what you quoted.
_TASK_NOTIFICATION_KIND = "task-notification"
_TASK_SUMMARY_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)
_TASK_STATUS_RE = re.compile(r"<status>\s*(.*?)\s*</status>", re.DOTALL)


def _task_notification_note(
    obj: dict[str, Any], text: str, ts: str,
) -> dict[str, Any] | None:
    """Collapse a task-notification envelope to a one-line system note, or
    None if this record isn't one."""
    origin = obj.get("origin")
    if not isinstance(origin, dict) or origin.get("kind") != _TASK_NOTIFICATION_KIND:
        return None
    summary = _TASK_SUMMARY_RE.search(text)
    status_m = _TASK_STATUS_RE.search(text)
    status = (status_m.group(1).strip() if status_m else "")
    line = summary.group(1).strip() if summary else "Background task finished"
    note: dict[str, Any] = {"type": "system_note", "ts": ts, "text": line}
    # "completed" is 387 of the 390 local records and the summary already reads
    # as success. Anything else (stopped / failed / killed) both gets said in
    # the text and is flagged, because 96% of these notes are routine and the
    # rare one that isn't has to be able to look different.
    if status and status != "completed":
        note["text"] = f"{line} [{status}]"
        note["level"] = "warning"
    return note

def _clean_user_text(text: str) -> str:
    """Strip Claude Code's local-command / slash-command scaffolding from a
    user message so the chat shows the intent, not the control markup."""
    if "<command-name>" not in text and "<local-command-" not in text:
        return _ANSI_RE.sub("", text)
    t = _CAVEAT_RE.sub("", text)
    if "<command-name>" in t:
        name = _CMD_NAME_RE.search(t)
        args = _CMD_ARGS_RE.search(t)
        if name:
            label = "/" + name.group(1).strip()
            arg = (args.group(1).strip() if args else "")
            # Drop the whole scaffold block, keep only a concise slash line.
            t = _CMD_TAG_RE.sub("", t)
            t = re.sub(r"<command-message>.*?</command-message>", "", t, flags=re.DOTALL)
            t = (label + (" " + arg if arg else "")).strip()
    t = _STDOUT_RE.sub("", t)
    t = _ANSI_RE.sub("", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    # Suppress the standalone /compact status line — pure noise, no bubble.
    if _COMPACT_NOISE_RE.match(t):
        return ""
    return t


def route_record(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """Split one transcript record dict into 0..N structured chat events.

    Detects codex rollout shape (`type: event_msg | session_meta |
    response_item`) vs claude JSONL shape (`message.role`) and dispatches
    accordingly. Both branches return the same event schema so the
    frontend renders identically regardless of which CLI produced it.
    """
    if not isinstance(obj, dict):
        return []
    rec_type = obj.get("type")
    ts = obj.get("timestamp") or obj.get("ts") or ""

    # ---- codex rollout dispatch ----
    if rec_type in ("event_msg", "session_meta", "response_item",
                    "turn_context", "world_state"):
        return _route_codex_record(obj, ts)

    # ---- claude JSONL dispatch ----
    msg = obj.get("message")
    if not isinstance(msg, dict):
        if rec_type == "summary" and isinstance(obj.get("summary"), str):
            return [{"type": "system_note", "ts": ts, "text": obj["summary"]}]
        return []
    content = msg.get("content")
    role = msg.get("role")
    blocks = _normalize_blocks(content)
    out: list[dict[str, Any]] = []
    for blk in blocks:
        bt = blk.get("type") if isinstance(blk, dict) else None
        if bt == "text":
            text = blk.get("text", "")
            if not text:
                continue
            if role == "assistant":
                out.append({"type": "assistant_text", "ts": ts, "text": text})
            else:
                note = _task_notification_note(obj, text, ts)
                if note is not None:
                    # A system event, not something anyone said — so it renders
                    # in the muted system style rather than as a bubble
                    # attributed to the user, and the jump rail (which indexes
                    # role "user") skips it without needing the flag below.
                    out.append(note)
                    continue
                cleaned = _clean_user_text(text)
                if cleaned:
                    evt: dict[str, Any] = {
                        "type": "user_message", "ts": ts, "text": cleaned,
                    }
                    if is_injected_user_record(obj, cleaned):
                        # Still rendered — it IS part of the transcript — but
                        # marked so surfaces that attribute it to the user (the
                        # bubble's own styling, the mobile jump rail) can leave
                        # it out.
                        evt["injected"] = True
                    out.append(evt)
        elif bt == "tool_use":
            out.append({
                "type": "tool_use_start",
                "ts": ts,
                "tool_id": blk.get("id", ""),
                "tool": blk.get("name", ""),
                "input": _truncate_input(blk.get("input", {})),
            })
        elif bt == "tool_result":
            preview = _result_preview(blk.get("content"))
            ok = not bool(blk.get("is_error"))
            out.append({
                "type": "tool_use_result",
                "ts": ts,
                "tool_id": blk.get("tool_use_id", ""),
                "ok": ok,
                "preview": preview,
            })
    return out


def _route_codex_record(obj: dict[str, Any], ts: str) -> list[dict[str, Any]]:
    """Codex rollout record → structured events.

    Chat bubbles come from `user_message` / `task_complete`; tool cards come
    from `response_item` (see `_route_codex_tool_record`). Records with no
    user-visible content (turn_context / world_state / token_count) return
    empty so the history replay stays clean.
    """
    rec_type = obj.get("type")
    if rec_type == "response_item":
        return _route_codex_tool_record(obj, ts)
    if rec_type == "session_meta":
        # Optional: surface as a system_note so the user sees "session
        # started with model=<x>". Keep small — most consumers won't want it.
        payload = obj.get("payload") or {}
        model = payload.get("model")
        if model:
            return [{
                "type": "system_note", "ts": ts,
                "text": f"[codex session started · model={model}]",
            }]
        return []
    if rec_type != "event_msg":
        return []
    payload = obj.get("payload") or {}
    if not isinstance(payload, dict):
        return []
    pt = payload.get("type")
    if pt == "user_message":
        text = str(payload.get("message") or "")
        if not text:
            return []
        return [{"type": "user_message", "ts": ts, "text": text}]
    if pt == "task_complete":
        text = str(payload.get("last_agent_message") or "")
        if not text:
            return []
        return [{"type": "assistant_text", "ts": ts, "text": text}]
    if pt == "turn_aborted":
        # The turn ended without a task_complete — say so, otherwise the
        # transcript just stops mid-thought with no explanation.
        reason = str(payload.get("reason") or "aborted")
        return [{"type": "system_note", "ts": ts, "text": f"[turn {reason}]"}]
    # token_count / task_started / agent_message etc. — not surfaced in the
    # chat feed. agent_message is deliberately skipped: task_complete's
    # `last_agent_message` is a verbatim copy of the final one (50/50 on
    # sampled rollouts), so emitting both would double every reply.
    return []


def _route_codex_tool_record(obj: dict[str, Any], ts: str) -> list[dict[str, Any]]:
    """Codex `response_item` → tool card halves.

    This is where "codex is doing something and the chat shows Thinking…"
    gets fixed. The model's tool calls live here, NOT in `event_msg`; routing
    only `event_msg` meant a turn emitted no chat events at all between the
    user's message and `task_complete`.

    The call and its output are SEPARATE records sharing `call_id`, so unlike
    the `*_end` records these give a real start→result pair: the card appears
    the moment codex starts the work and fills in when it lands.

    Deliberately NOT paired with `patch_apply_end` / `web_search_end`. Those
    describe the inner steps of one such call (measured 1:1 with the wrapping
    `exec` on 30 real rollouts), so routing both rendered the same action
    twice. One card per model tool call is the rule.
    """
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    pt = payload.get("type")

    if pt in ("function_call", "custom_tool_call"):
        mapped = codex_tool_call(payload)
        if mapped is None:
            return []
        tool, tool_input = mapped
        return [{
            "type": "tool_use_start",
            "ts": ts,
            "tool_id": str(payload.get("call_id") or ""),
            "tool": tool,
            "input": _truncate_input(tool_input),
        }]

    if pt in ("function_call_output", "custom_tool_call_output"):
        tool_id = str(payload.get("call_id") or "")
        if not tool_id:
            # Without an id the frontend can't pair it to a card, and an
            # orphan result renders as a stray empty row.
            return []
        return [{
            "type": "tool_use_result",
            "ts": ts,
            "tool_id": tool_id,
            # Codex has no per-call error flag; a failed command reports
            # through its output text like any other result.
            "ok": True,
            "preview": _result_preview(payload.get("output")),
        }]

    # reasoning / message / other response_items carry no chat content.
    return []


def _truncate_input(value: Any, limit: int = 2000) -> Any:
    """Cap long string values inside a tool_use `input` so a single Write/Edit
    carrying an entire file body doesn't bloat the history frame (the paired
    tool_result already caps at 2000 — this gives the request side parity).
    Walks dicts/lists one level deep; marks truncation so the UI can show it.
    """
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"… [+{len(value) - limit} chars truncated]"
        return value
    if isinstance(value, dict):
        return {k: _truncate_input(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [_truncate_input(v, limit) for v in value]
    return value


def _normalize_blocks(content: Any) -> list[Any]:
    if isinstance(content, list):
        return content
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def _result_preview(content: Any, limit: int = 2000) -> str:
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, dict):
                if isinstance(blk.get("text"), str):
                    parts.append(blk["text"])
                elif blk.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(blk, str):
                parts.append(blk)
        out = "\n".join(parts)
        return out[:limit]
    if content is None:
        return ""
    return str(content)[:limit]
