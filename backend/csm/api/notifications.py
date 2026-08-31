"""REST + WebSocket endpoints for Notification Bus."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import _require_access_ws, get_db_sessionmaker
from csm.api._serialize import iso_utc
from csm.core.notification_bus import NotificationBus
from csm.models import Session as SessionModel

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


def _bus(request: Request) -> NotificationBus:
    bus = getattr(request.app.state, "notification_bus", None)
    if bus is None:
        raise HTTPException(status_code=503, detail="notification bus not initialized")
    return bus


def _serialize(n, title_by_sid: dict[str, str] | None = None) -> dict[str, Any]:
    meta = dict(n.notif_metadata or {})
    # Fix for local:73989b61 — notification meta snapshots session_title at
    # write time, so a session rename desynchronizes existing notifications.
    # Overlay the CURRENT Session.title (looked up in the batch below); fall
    # back to the snapshot if the session has since been purged, so orphaned
    # notifications still show something meaningful.
    if title_by_sid is not None and n.session_id and n.session_id in title_by_sid:
        meta["session_title"] = title_by_sid[n.session_id]
    return {
        "id": n.id,
        "type": n.type.value if hasattr(n.type, "value") else n.type,
        "session_id": n.session_id,
        "title": n.title,
        "body": n.body,
        "created_at": iso_utc(n.created_at),
        "read_at": iso_utc(n.read_at),
        "dismissed_at": iso_utc(n.dismissed_at),
        "metadata": meta,
    }


async def _fetch_session_titles(
    sm: async_sessionmaker, session_ids: list[str]
) -> dict[str, str]:
    """Batch fetch current Session.title for a set of session ids.

    Returns a mapping only for sessions that still exist. Missing ids
    (session purged) simply won't appear in the dict — the caller then
    falls back to the snapshot in notif_metadata.
    """
    if not session_ids:
        return {}
    async with sm() as db:
        rows = (await db.execute(
            select(SessionModel.id, SessionModel.title).where(
                SessionModel.id.in_(session_ids)
            )
        )).all()
    return {sid: title for sid, title in rows if title}


@router.get("")
async def list_notifications(
    request: Request,
    limit: int = 100,
    only_unread: bool = False,
    include_dismissed: bool = False,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    bus = _bus(request)
    rows = await bus.list_notifications(limit=limit, only_unread=only_unread, include_dismissed=include_dismissed)
    session_ids = list({r.session_id for r in rows if r.session_id})
    title_by_sid = await _fetch_session_titles(sm, session_ids)
    return {"count": len(rows), "items": [_serialize(r, title_by_sid) for r in rows]}


@router.get("/unread-summary")
async def unread_summary(request: Request):
    bus = _bus(request)
    total, by_session = await asyncio.gather(
        bus.total_unread(),
        bus.unread_by_session(),
    )
    return {"total_unread": total, "by_session": by_session}


@router.post("/{nid}/read")
async def mark_read(nid: str, request: Request):
    bus = _bus(request)
    ok = await bus.mark_read(nid)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"read": nid}


@router.post("/{nid}/dismiss")
async def dismiss(nid: str, request: Request):
    bus = _bus(request)
    ok = await bus.dismiss(nid)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"dismissed": nid}


@router.post("/mark-session-read/{session_id}")
async def mark_session_read(session_id: str, request: Request):
    bus = _bus(request)
    cleared = await bus.mark_session_read(session_id)
    if cleared == 0:
        raise HTTPException(status_code=404, detail="session not found")
    return {"cleared_session": session_id}


@router.post("/clear-all")
async def clear_all(request: Request):
    """One-shot: dismiss every notification + zero every session unread."""
    bus = _bus(request)
    return await bus.mark_all_read()


@router.websocket("/ws")
async def attach(websocket: WebSocket):
    _require_access_ws(websocket)
    sink = websocket.app.state.inapp_sink
    await sink.attach(websocket)
    try:
        while True:
            # We only push from the server, but honour the client's heartbeat:
            # it sends {"type":"ping"} while idle and closes the socket if no
            # reply lands, so a silently-dropped connection reconnects instead
            # of sitting dead (mirrors the terminal WS heartbeat).
            text = await websocket.receive_text()
            if not text:
                continue
            is_ping = False
            try:
                is_ping = json.loads(text).get("type") == "ping"
            except Exception:
                is_ping = text.strip() == "ping"
            if is_ping:
                try:
                    await websocket.send_text('{"type": "pong"}')
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        sink.detach(websocket)
