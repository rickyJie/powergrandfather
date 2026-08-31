"""REST endpoints for the worktime widget in the header top-right.

Two endpoints:
- `POST /api/worktime/heartbeat` — frontend `useWorktimeHeartbeat` calls
  this every 30s while the tab is visible AND has seen a mouse/keyboard
  event in the last 120s.
- `GET  /api/worktime/live` — widget polls this every 5s to render
  today's totals plus the currently-ticking open-agent seconds.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from csm.modules.worktime import HeartbeatManager, WorktimeService

router = APIRouter(prefix="/api/worktime", tags=["worktime"])


def _hb(request: Request) -> HeartbeatManager:
    return request.app.state.worktime_heartbeat


def _svc(request: Request) -> WorktimeService:
    return request.app.state.worktime_service


@router.post("/heartbeat")
async def post_heartbeat(request: Request) -> dict[str, Any]:
    return await _hb(request).heartbeat()


@router.get("/live")
async def get_live(request: Request) -> dict[str, Any]:
    totals = await _svc(request).live_totals()
    return {
        "today_human_sec": totals.today_human_sec,
        "today_agent_sec": totals.today_agent_sec,
        "all_human_sec": totals.all_human_sec,
        "all_agent_sec": totals.all_agent_sec,
        "open_agent_sec": totals.open_agent_sec,
        "open_human_sec": totals.open_human_sec,
        "open_agent_count": totals.open_agent_count,
        "day_bucket_utc": totals.day_bucket_utc,
    }
