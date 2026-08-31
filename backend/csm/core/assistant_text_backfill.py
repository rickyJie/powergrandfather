"""One-shot backfill for Session.last_assistant_msg (local:5de334d5).

The v1 code populated `last_assistant_msg` only from the `Stop` hook in
`csm/api/hooks.py`, which reads the transcript file via
`_read_last_assistant_text(transcript_path)`. In practice this landed in
~25% of sessions — the hook's `transcript_path` payload was flaky on
some claude versions, and its path-resolution guard rejected any path
not exactly under `settings.claude_projects_dir` (symlinks, `~` prefixes,
version differences all silently failed).

The fix is two-part:
  1. Forward-fix: EventStream extracts assistant text from the JSONL tail
     and includes it in `MESSAGE_ASSISTANT_DONE.payload["assistant_text"]`.
     NotificationBus writes it to `sess.last_assistant_msg` under its
     existing per-session lock. Every new assistant turn is covered.
  2. Backfill: this module. On backend startup, scan every interactive
     session with `last_assistant_msg IS NULL` that still has its JSONL
     on disk, read the tail, and populate. One-shot, fire-and-forget.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import Session
from csm.models.session import SessionType
from csm.modules.agent.jsonl_fast_tail import conversation_jsonl_path

log = logging.getLogger("csm.backfill.assistant_text")


def _extract_last_assistant_text(jsonl_path: Path) -> str | None:
    """Read a JSONL transcript top-to-bottom, return the last assistant
    text turn (concatenated `content[type=text].text` blocks). Returns None
    on any read/parse failure — this is a best-effort backfill, not
    critical-path.
    """
    try:
        if not jsonl_path.is_file():
            return None
    except OSError:
        return None
    last_text: str | None = None
    try:
        with jsonl_path.open("rb") as f:
            for raw in f:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else None
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)
                    ]
                    if parts:
                        last_text = "".join(parts)
    except OSError:
        return None
    if last_text:
        return last_text[:2000]
    return None


async def backfill_last_assistant_msg(
    sessionmaker: async_sessionmaker[Any],
    claude_projects_dir: str | Path,
) -> None:
    """Sweep every interactive session missing `last_assistant_msg` and
    fill it from the on-disk JSONL. Idempotent; safe to run repeatedly.
    """
    try:
        candidates: list[tuple[str, str, str]] = []
        # Multi-agent v2: backfill parses claude JSONL specifically
        # (`~/.claude/projects/<cwd>/<uuid>.jsonl`), so scope to claude
        # sessions. Codex last-assistant-msg needs a separate rollout-
        # file backfill (deferred). The literal below is the adapter
        # name — allowed here because this file is an adapter-specific
        # implementation, not domain code (see docs/backends/canonical_events.md).
        _CLAUDE_ONLY_BACKFILL = "claude"
        async with sessionmaker() as db:
            res = await db.execute(
                select(Session.id, Session.cwd, Session.external_session_id).where(
                    Session.type == SessionType.INTERACTIVE,
                    Session.last_assistant_msg.is_(None),
                    Session.external_session_id.isnot(None),
                    Session.agent == _CLAUDE_ONLY_BACKFILL,
                )
            )
            candidates = [(sid, cwd, csid) for sid, cwd, csid in res.all()]
        if not candidates:
            return
        log.info("[backfill] scanning %d sessions for last_assistant_msg", len(candidates))
        updated = 0
        for sid, cwd, csid in candidates:
            try:
                path = conversation_jsonl_path(str(claude_projects_dir), cwd, csid)
            except Exception:
                continue
            text = _extract_last_assistant_text(path)
            if not text:
                continue
            try:
                async with sessionmaker() as db:
                    row = await db.get(Session, sid)
                    # Re-check inside write session: another path may have
                    # populated the field between our select and now (Stop
                    # hook races EventStream); don't clobber.
                    if row is None or row.last_assistant_msg:
                        continue
                    row.last_assistant_msg = text
                    await db.commit()
                    updated += 1
            except Exception:
                log.exception("[backfill] failed to update sid=%s", sid)
        log.info("[backfill] populated last_assistant_msg on %d/%d sessions", updated, len(candidates))
    except Exception:
        log.exception("[backfill] top-level failure — skipping")
