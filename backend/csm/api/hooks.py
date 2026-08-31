"""Claude Code hooks callback endpoint (H4).

Every spawned claude session is started with `--settings` pointing each of the 6
hook events to `POST /api/hooks/{sid}`. The body is the official Claude Code
hook payload (JSON via stdin equivalent). We dispatch by `hook_event_name` and
update the matching `Session` row's state + push an EventStream event for
downstream consumers (NotificationBus, frontend).

Response shape: we just return `{}` (no permission override). If we ever want
to programmatically block a tool, we can return the
`{"hookSpecificOutput": {...}}` shape Claude Code expects.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask

from csm.api._deps import _require_loopback_and_host
from csm.api.sessions import _jsonl_present
from csm.config import settings
from csm.core.events import Event, EventType
from csm.models.session import Session, SessionStatus
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)

# When a spawned claude subprocess fires SessionStart/SessionEnd hooks so fast
# that create_session() hasn't finished committing the Session row yet,
# db.get() returns None and the event is silently dropped — the root cause
# behind the "zombie running row" bug where CSM never learns the child died.
# We poll briefly to cover the ~100-500ms commit gap. Beyond that, the row
# truly doesn't exist (e.g. purged mid-flight).
# How much of the tail of a transcript `_read_last_assistant_text` looks at.
# Comfortably more than one turn's worth of JSONL, and bounded regardless of
# how long the session has been running.
_TRANSCRIPT_TAIL_BYTES = 512 * 1024
_UNKNOWN_SID_RETRY_ATTEMPTS = 6
_UNKNOWN_SID_RETRY_DELAY_SEC = 0.15

# `SessionEnd` reasons that are an in-place context reset — the PTY process
# stays alive and immediately re-inits via a `SessionStart`, so this is NOT a
# real termination. New claude versions fire `SessionEnd(reason="clear")` on
# `/clear`; retiring the row on it (EXITED + SESSION_ENDED) made the session
# vanish from the active list on a mere context clear. Real exits still get
# retired: they either send a non-soft reason OR the PTY dies and
# SessionManager's reaper marks the row terminal independently.
_SOFT_SESSION_END_REASONS = {"clear"}
# `SessionStart` sources where the user/agent deliberately rotated the
# transcript in-place. Adopting the new claude_session_id is correct here even
# though the old JSONL still has content, so these bypass the half-born-claude
# guard (`_old_sid_is_replaceable`) that otherwise refuses the rebind.
_INPLACE_RESTART_SOURCES = {"clear", "compact"}


def _old_sid_is_replaceable(cwd: str, old_sid: str | None) -> bool:
    """Guard for the SessionStart claude_session_id overwrite.

    Reuses `_jsonl_present` (which now checks existence + real content),
    so the two callers — Resume preflight and this hook — share one
    definition of "resumable". Returns True when the old sid is safe to
    replace (unset, or its JSONL is missing/empty).
    """
    if not old_sid:
        return True
    return not _jsonl_present(cwd, old_sid)


router = APIRouter(prefix="/api/hooks", tags=["hooks"])


@router.post("/{sid}")
async def receive_hook(sid: str, request: Request) -> Any:
    """Receive a Claude Code hook callback. `sid` is our CSM Session.id.

    Contract: **any internal failure returns 200 {}**. Claude Code treats
    non-2xx hook responses as failure and blocks the session waiting for a
    retry; a Finding-5 incident showed a 502 from this endpoint left a
    real claude REPL hung for 10h. Loopback-check 403 is preserved (it is
    an external boundary, not an internal error).
    """
    _require_loopback_and_host(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    event_name = body.get("hook_event_name") or "(unknown)"
    log.debug("hook %s for sid=%s", event_name, sid)

    try:
        sm = request.app.state.sessionmaker
        sess = None
        # Retry the row lookup for up to ~1s. When a resumed claude dies
        # immediately (bad --resume sid → JSONL missing → subprocess exits
        # before create_session() commits) the SessionEnd hook can arrive
        # before the row is durable. Without this retry the event silently
        # drops → row stays at `running` with a dead PID → user's next
        # Resume attempt on the predecessor gets blocked because the (dead)
        # successor row still claims the superseded_by link.
        for _ in range(_UNKNOWN_SID_RETRY_ATTEMPTS):
            async with sm() as probe_db:
                sess = await probe_db.get(Session, sid)
            if sess is not None:
                break
            await asyncio.sleep(_UNKNOWN_SID_RETRY_DELAY_SEC)
        if sess is None:
            # Row genuinely gone (purged / never existed / process outlived
            # a legitimate delete). Nothing sensible to do — log at warn
            # to preserve visibility.
            log.warning("hook for unknown session sid=%s event=%s", sid, event_name)
            return {}

        async with sm() as db:
            sess = await db.get(Session, sid)
            if sess is None:
                # Second-race: got purged between retry above and now. Treat
                # as unknown (already warned above wouldn't have fired).
                log.warning("hook for unknown session sid=%s event=%s (raced away)", sid, event_name)
                return {}
            await _dispatch(db, sess, event_name, body)
            try:
                await db.commit()
            except IntegrityError:
                # SessionStart overwrite of claude_session_id can hit
                # ux_session_claude_sid_active if another live session
                # already claims the new id. Roll back the write but keep
                # the row otherwise coherent — reader task will still
                # detect the child death and mark CRASHED.
                await db.rollback()
                log.warning(
                    "hook %s for sid=%s hit unique-constraint on external_session_id; "
                    "keeping stale value (concurrent live session owns the new id)",
                    event_name, sid,
                )
            claude_sid = sess.external_session_id
            cwd = sess.cwd

        # Push to EventStream so subscribers (NotificationBus, frontend WS)
        # react — but do NOT await it inline. Claude Code BLOCKS the session on
        # this hook's HTTP response, and `EventStream.emit` awaits every
        # subscriber via `asyncio.gather` (NotificationBus rebind + jsonl stat
        # scans + its own SQLite commit contending on the single writer, and in
        # the worst case SupervisorAgent's `claude -p`, 10-60s). Awaiting it here
        # made the `Stop` hook take 9-13s (measured in perf.log), so a session
        # that had finished still showed "agent working" until the fan-out
        # returned. The status flip is ALREADY committed above, so the client
        # can be unblocked immediately; run the fan-out after the response
        # flushes via a BackgroundTask.
        es = getattr(request.app.state, "event_stream", None)
        if es is not None:
            return JSONResponse(
                {},
                background=BackgroundTask(_emit, es, sid, claude_sid, cwd, event_name, body),
            )
    except Exception:
        # Never leak internal errors back to claude — that hangs the session.
        log.exception("hook handler failed sid=%s event=%s (swallowed, returning 200)", sid, event_name)
    return {}


async def _dispatch(db, sess: Session, event_name: str, body: dict[str, Any]) -> None:
    """Mutate Session in-place based on hook event."""
    now = now_utc_naive()
    sess.last_activity_ts = now
    # ORPHANED means this backend instance does not own a PTY handle. The
    # reparented Claude process can still deliver hooks after a backend
    # restart, but those hooks must not advertise an interactive RUNNING/IDLE
    # state to the frontend. Only SessionEnd may leave ORPHANED.
    pty_unavailable = sess.status == SessionStatus.ORPHANED
    # Any non-SessionEnd hook = claude process is alive → ended_at must be
    # null. Fixes the split-brain "status=running + ended_at set" state that
    # occurred when SessionEnd fired but the process kept running (or
    # re-entered), leaving the row permanently inconsistent.
    if event_name != "SessionEnd" and not pty_unavailable:
        sess.ended_at = None

    if event_name == "SessionStart":
        # SessionStart fires when claude finishes init, before the user
        # has submitted anything — the agent isn't working yet. Set IDLE
        # so the UI shows "waiting for input". UserPromptSubmit/PreToolUse
        # promote to RUNNING when a real turn starts.
        if not pty_unavailable:
            sess.status = SessionStatus.IDLE
        # Guarded sid overwrite. History:
        # v1 — only wrote when current sid was None. Broke Resume when
        #      the initial guess pointed at a JSONL claude had rotated.
        # v2 — always overwrote if the two sids differed. Fixed v1 but
        #      introduced the openpi incident: a claude that crashes
        #      500ms after spawn (only writes permission-mode) can
        #      clobber a healthy long-lived sid, orphaning the real
        #      transcript. Six repeat Resume clicks compounded it.
        # v3 (this) — overwrite only when the OLD sid is unusable:
        #      unset, its JSONL missing, OR its JSONL empty (only meta
        #      lines). Keeps v1's "correct a stale pointer" behaviour
        #      but refuses to trade a healthy transcript for a
        #      half-born one.
        incoming_sid = body.get("session_id")
        source = (body.get("source") or "").lower()
        if isinstance(incoming_sid, str) and incoming_sid and incoming_sid != sess.external_session_id:
            # `source in {clear, compact}` = the user/agent deliberately rotated
            # the transcript in this same PTY; the old JSONL having content is
            # expected, not a red flag, so adopt the new sid unconditionally.
            # Otherwise fall back to the half-born-claude guard.
            if source in _INPLACE_RESTART_SOURCES or _old_sid_is_replaceable(
                sess.cwd, sess.external_session_id
            ):
                log.info(
                    "hook SessionStart for sid=%s (source=%s): external_session_id %s → %s",
                    sess.id, source or "?", sess.external_session_id, incoming_sid,
                )
                sess.external_session_id = incoming_sid
            else:
                log.warning(
                    "hook SessionStart for sid=%s: refused to overwrite healthy "
                    "external_session_id=%s with incoming=%s (old JSONL has content)",
                    sess.id, sess.external_session_id, incoming_sid,
                )

    elif event_name == "UserPromptSubmit":
        if not pty_unavailable:
            sess.status = SessionStatus.RUNNING

    elif event_name == "PreToolUse":
        tool = body.get("tool_name") or ""
        # Surface tool + a one-line input hint for the card (e.g. Bash command head).
        tin = body.get("tool_input") or {}
        hint = ""
        for k in ("command", "file_path", "path", "url", "pattern"):
            if isinstance(tin.get(k), str):
                hint = tin[k][:80]
                break
        sess.current_tool = f"{tool}: {hint}" if hint else tool
        if not pty_unavailable:
            sess.status = SessionStatus.RUNNING
        # Record file writes for the "📄 Files" popover.
        # Only the four tools that actually write to a specific path are
        # tracked — Read/Grep/Glob don't produce artifacts users would
        # want to click into. Prune-to-100 keeps the per-session set
        # bounded so a long-running session doesn't unbounded-grow.
        if tool in ("Write", "Edit", "MultiEdit", "Create"):
            fp = tin.get("file_path") or tin.get("path")
            if isinstance(fp, str) and fp:
                from csm.api.files import prune_session_file_touches
                from csm.models import SessionFileTouch
                db.add(SessionFileTouch(sid=sess.id, path=fp[:2048], tool=tool[:32]))
                try:
                    await db.flush()  # so the INSERT is visible to the prune query
                    await prune_session_file_touches(db, sess.id, keep=100)
                except Exception:
                    log.exception("file_touch prune failed sid=%s", sess.id)

    elif event_name == "Notification":
        ntype = body.get("notification_type") or ""
        if ntype == "permission_prompt":
            if not pty_unavailable:
                sess.status = SessionStatus.WAITING_AUTH
            sess.unread_count = (sess.unread_count or 0) + 1
        elif ntype == "idle_prompt":
            if not pty_unavailable:
                sess.status = SessionStatus.WAITING_INPUT

    elif event_name == "Stop":
        sess.current_tool = None
        # Stop hook fires when claude finishes an assistant turn —
        # control is back to the user. The state is IDLE ("waiting for
        # user, no explicit prompt"), NOT RUNNING. Setting RUNNING here
        # (the v1 bug) left sessions stuck at running forever because
        # nothing else transitioned back, unless claude happened to emit
        # a Notification:idle_prompt (unreliable). See feedback 4965351a.
        if not pty_unavailable:
            sess.status = SessionStatus.IDLE
        # Try to pull the last assistant text from the transcript.
        tpath = body.get("transcript_path")
        if isinstance(tpath, str):
            # Deliberately inline, NOT `asyncio.to_thread`. Bounding the read to
            # the tail (see `_read_last_assistant_text`) took the worst observed
            # case from 137.8ms to 2.7ms on this box's largest transcript
            # (25.2 MB) — 98% of the win, and 2.7ms is under the noise floor of
            # a request that is already doing DB work. Handing that off would
            # cost a thread round-trip while this coroutine holds a DB session,
            # and would spend a slot in the default executor, which
            # `config.py:179` records as having occupants that cannot be killed.
            text = _read_last_assistant_text(tpath)
            if text:
                sess.last_assistant_msg = text[:2000]
        # NOTE: unread_count is deliberately NOT bumped here — we emit a
        # MESSAGE_ASSISTANT_DONE via `_emit` below, and NotificationBus._on_assistant_done
        # is the single source of truth for counting. Bumping here caused +2
        # per turn (once here, once in the bus subscriber).

    elif event_name == "SessionEnd":
        reason = (body.get("reason") or "").lower()
        if reason in _SOFT_SESSION_END_REASONS:
            # In-place reset (`/clear`): the process is alive and a SessionStart
            # follows immediately. Don't retire the row — leave status/ended_at
            # untouched so the session stays in the active list.
            log.info(
                "hook SessionEnd for sid=%s: soft reason=%s, not retiring (in-place reset)",
                sess.id, reason,
            )
            return
        sess.ended_at = now
        sess.status = SessionStatus.EXITED
        sess.current_tool = None


def _read_last_assistant_text(transcript_path: str) -> str | None:
    """Best-effort: pull the last assistant text block out of the transcript.

    Reads only the tail. The previous version scanned and JSON-parsed the whole
    file on every Stop hook, on the assumption (in the old comment here) that
    transcripts are "<1 MB per turn" — but a transcript accumulates for the life
    of the SESSION, not the turn: the largest ones on this box are 19-25 MB, and
    parsing one costs ~150-200 ms.

    Losing the text when the tail window happens to hold no assistant message
    (one enormous tool result) is acceptable: `Session.last_assistant_msg` is
    also written from the JSONL-derived MESSAGE_ASSISTANT_DONE payload, which
    NotificationBus treats as the authoritative source anyway.
    """
    try:
        try:
            p = Path(transcript_path).resolve()
            root = Path(settings.claude_projects_dir).resolve()
        except Exception:
            return None
        if not p.is_relative_to(root):
            return None
        if not p.exists():
            return None
        last_text: str | None = None
        with p.open("rb") as f:
            size = p.stat().st_size
            if size > _TRANSCRIPT_TAIL_BYTES:
                f.seek(size - _TRANSCRIPT_TAIL_BYTES)
                f.readline()  # drop the partial line the seek landed inside
            for raw in f:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
                if msg.get("role") != "assistant":
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    last_text = content
                elif isinstance(content, list):
                    parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                    if parts:
                        last_text = "".join(parts)
        return last_text
    except Exception:
        return None


async def _emit(es, sid: str, claude_sid: str | None, cwd: str | None, event_name: str, body: dict[str, Any]) -> None:
    """Translate hook event → EventStream Event so other modules can react."""
    now = datetime.now(UTC)
    # Mirror `_dispatch`: a soft SessionEnd (`/clear`) is not a real end, so
    # don't emit SESSION_ENDED — that's what makes subscribers (frontend card
    # list, NotificationBus auto-mark-read) treat the session as gone.
    if event_name == "SessionEnd" and (body.get("reason") or "").lower() in _SOFT_SESSION_END_REASONS:
        return
    type_map = {
        "SessionStart": EventType.SESSION_STARTED,
        "Stop": EventType.MESSAGE_ASSISTANT_DONE,
        "UserPromptSubmit": EventType.MESSAGE_USER_SENT,
        "PreToolUse": EventType.SESSION_TOOL_PROGRESS,
        "SessionEnd": EventType.SESSION_ENDED,
    }
    et = type_map.get(event_name)
    if event_name == "Notification":
        ntype = (body.get("notification_type") or "")
        if ntype == "permission_prompt":
            et = EventType.SESSION_WAITING_AUTH
        elif ntype == "idle_prompt":
            et = EventType.SESSION_WAITING_INPUT
        else:
            return
    if et is None:
        return
    payload = {"hook_event_name": event_name, "csm_session_id": sid}
    for k in ("tool_name", "tool_input", "prompt", "notification_type", "reason", "transcript_path"):
        if k in body:
            payload[k] = body[k]
    await es.emit(Event(
        type=et,
        ts=now,
        session_id=claude_sid,
        project_path=cwd,
        payload=payload,
    ))
