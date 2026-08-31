"""Claude JSONL → canonical CSM Event derivation.

Extracted from `csm.core.event_stream._handle_record` so ClaudeAdapter can
own the mapping. M3 will delete the copy in event_stream and route through
this module.

Contract:
    - Pure per-record (no cross-record state accumulated *inside* this
      module; the caller — ClaudeAdapter — is responsible for msg_count
      / SESSION_STARTED gating and any per-file bookkeeping).
    - Returns a list (not a generator) so the caller can `.extend()`
      without materialising a temporary.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from csm.adapters.jsonl_tail import RawRecord
from csm.core.events import Event, EventType
from csm.core.transcript_provenance import is_injected_user_record

_HIT_LIMIT_RE = re.compile(r"hit your limit", re.IGNORECASE)
_RESET_RE = re.compile(
    r"resets\s+(\d{1,2}:\d{2})\s*(am|pm)?\s*\(([^)]+)\)", re.IGNORECASE
)


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and "text" in c and isinstance(c["text"], str):
                parts.append(c["text"])
        return "\n".join(parts)
    return ""


def derive_claude_events(r: RawRecord) -> list[Event]:
    """Turn one JSONL record into zero or more canonical Events."""
    events: list[Event] = []
    obj = r.obj
    ts = _parse_ts(obj.get("timestamp")) or datetime.now(UTC)
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    role = msg.get("role")

    # ---- API error / rate limit ----
    if obj.get("isApiErrorMessage"):
        content = msg.get("content") if isinstance(msg, dict) else None
        text = _extract_text(content) if content is not None else ""
        events.append(Event(
            type=EventType.API_ERROR,
            ts=ts,
            session_id=r.claude_session_id,
            project_path=r.project_path,
            payload={"text": text[:500]},
            source_offset=r.byte_offset,
        ))
        if _HIT_LIMIT_RE.search(text):
            m = _RESET_RE.search(text)
            reset = f"{m.group(1)}{m.group(2) or ''} {m.group(3)}" if m else None
            events.append(Event(
                type=EventType.RATE_LIMIT_HIT,
                ts=ts,
                session_id=r.claude_session_id,
                project_path=r.project_path,
                payload={"reset_text": reset, "raw": text[:500]},
                source_offset=r.byte_offset,
            ))

    # ---- user message + embedded tool_result blocks ----
    if role == "user":
        # Same gate as EventStream's inline derivation: role "user" covers
        # subagent task-notifications, skill preambles and SDK-driven prompts,
        # none of which are the human speaking. See
        # `csm.core.transcript_provenance`.
        if not is_injected_user_record(obj, _extract_text(msg.get("content"))):
            events.append(Event(
                type=EventType.MESSAGE_USER_SENT,
                ts=ts,
                session_id=r.claude_session_id,
                project_path=r.project_path,
                payload={},
                source_offset=r.byte_offset,
            ))
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    events.append(Event(
                        type=EventType.TOOL_COMPLETED,
                        ts=ts,
                        session_id=r.claude_session_id,
                        project_path=r.project_path,
                        payload={"tool_use_id": c.get("tool_use_id")},
                        source_offset=r.byte_offset,
                    ))
        return events

    # ---- assistant: usage + tool_use blocks + end_turn ----
    if role == "assistant":
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if usage:
            events.append(Event(
                type=EventType.USAGE_RECORDED,
                ts=ts,
                session_id=r.claude_session_id,
                project_path=r.project_path,
                payload={
                    "model": msg.get("model"),
                    "input_tokens": usage.get("input_tokens", 0),
                    "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
                    "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "is_subagent": "/subagents/" in r.jsonl_path,
                },
                source_offset=r.byte_offset,
            ))

        content = msg.get("content") if isinstance(msg, dict) else None
        tool_names: list[str] = []
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    nm = c.get("name")
                    if nm:
                        tool_names.append(str(nm))
        if tool_names:
            n = len(tool_names)
            u = usage or {}
            share = {
                "input_tokens": int(u.get("input_tokens", 0) or 0) // n,
                "cache_creation_input_tokens":
                    int(u.get("cache_creation_input_tokens", 0) or 0) // n,
                "cache_read_input_tokens":
                    int(u.get("cache_read_input_tokens", 0) or 0) // n,
                "output_tokens": int(u.get("output_tokens", 0) or 0) // n,
            }
            model = msg.get("model")
            for nm in tool_names:
                events.append(Event(
                    type=EventType.TOOL_INVOKED,
                    ts=ts,
                    session_id=r.claude_session_id,
                    project_path=r.project_path,
                    payload={
                        "name": nm,
                        "jsonl_path": r.jsonl_path,
                        "model": model,
                        **share,
                    },
                    source_offset=r.byte_offset,
                ))

        # Only emit MESSAGE_ASSISTANT_DONE on end_turn (else "still going").
        stop_reason = msg.get("stop_reason") if isinstance(msg, dict) else None
        if stop_reason == "end_turn":
            text_parts: list[str] = []
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "text":
                        t = c.get("text", "")
                        if isinstance(t, str) and t:
                            text_parts.append(t)
            assistant_text = ("".join(text_parts))[:2000] if text_parts else None
            events.append(Event(
                type=EventType.MESSAGE_ASSISTANT_DONE,
                ts=ts,
                session_id=r.claude_session_id,
                project_path=r.project_path,
                payload={"model": msg.get("model"), "assistant_text": assistant_text},
                source_offset=r.byte_offset,
            ))

    return events


__all__ = ["derive_claude_events"]
