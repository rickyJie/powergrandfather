"""REST endpoints for the Automation module (workflow-only after P2).

P2 retired M4 TaskDefinition. Endpoints exposed here now cover:
- **Schedules**: cron / one-shot schedules targeting a WorkflowDefinition
  (task_def_id is column-dropped in migration g9b2c3d4e5f6).
- **Runs**: per-stage execution records for workflow missions. Get / list /
  cancel / retry (the launch entry is gone — missions are launched via
  `POST /api/missions/launch`).
- **Output raw**: serve a stage-produced file (bounded, UTF-8 preferred).

Everything M4 (probe / task CRUD / task review / task_loader reload) is
gone from this surface; matching routes will 404.
"""
from __future__ import annotations

from datetime import UTC
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.api._serialize import iso_utc
from csm.models import Output, Run, ScheduleEntry

router = APIRouter(prefix="/api", tags=["automation"])


# ---- helpers ----
def _se_dict(se: ScheduleEntry) -> dict[str, Any]:
    return {
        "id": se.id,
        "workflow_def_id": se.workflow_def_id,
        "cron": se.cron,
        "run_at": iso_utc(se.run_at),
        "kind": "once" if se.run_at is not None else "recurring",
        "enabled": se.enabled,
        "parameters": se.parameters or {},
        "next_run_at": iso_utc(se.next_run_at),
        "last_run_at": iso_utc(se.last_run_at),
    }


def _run_dict(r: Run) -> dict[str, Any]:
    return {
        "id": r.id,
        "mission_id": getattr(r, "mission_id", None),
        "stage_name": getattr(r, "stage_name", None),
        "schedule_entry_id": r.schedule_entry_id,
        "session_id": r.session_id,
        "status": r.status.value if hasattr(r.status, "value") else r.status,
        "started_at": iso_utc(r.started_at),
        "ended_at": iso_utc(r.ended_at),
        "exit_code": r.exit_code,
        "parameters": r.parameters or {},
        "review_note": r.review_note,
    }


def _runner(request: Request):
    r = getattr(request.app.state, "automation_runner", None)
    if r is None:
        raise HTTPException(status_code=503, detail="automation runner not initialized")
    return r


def _sched(request: Request):
    s = getattr(request.app.state, "automation_scheduler", None)
    if s is None:
        raise HTTPException(status_code=503, detail="automation scheduler not initialized")
    return s


# ---- schedules ----
class CreateScheduleBody(BaseModel):
    """Schedule target must be a WorkflowDefinition (M8) — M4 is gone.

    Timing: exactly one of `cron` (recurring 5-field) or `run_at` (one-shot ISO 8601 UTC).
    """
    workflow_def_id: str
    cron: str | None = None
    run_at: str | None = None     # ISO 8601 (UTC) for one-shot schedules
    parameters: dict[str, Any] | None = None


@router.get("/schedules")
async def list_schedules(
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as db:
        rows = (await db.execute(select(ScheduleEntry))).scalars().all()
        return {"count": len(rows), "items": [_se_dict(r) for r in rows]}


@router.post("/schedules")
async def create_schedule(body: CreateScheduleBody, request: Request):
    """Create a schedule bound to a workflow. Fires `launch_mission` on tick."""
    from datetime import datetime as _dt
    if (body.cron is None) == (body.run_at is None):
        raise HTTPException(status_code=400, detail="provide exactly one of cron / run_at")
    run_at: _dt | None = None
    if body.run_at is not None:
        try:
            # Accept both "...Z" and naive ISO. Always normalize to naive UTC.
            s = body.run_at.replace("Z", "+00:00")
            run_at = _dt.fromisoformat(s)
            if run_at.tzinfo is not None:
                run_at = run_at.astimezone(UTC).replace(tzinfo=None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid run_at: {e}") from e
    sched = _sched(request)
    try:
        row = await sched.add_schedule(
            workflow_def_id=body.workflow_def_id,
            cron_expr=body.cron,
            run_at=run_at,
            parameters=body.parameters or {},
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _se_dict(row)


class UpdateScheduleBody(BaseModel):
    cron: str | None = None
    run_at: str | None = None


@router.patch("/schedules/{sid}")
async def update_schedule(
    sid: str,
    body: UpdateScheduleBody,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Replace the timing of an existing schedule.

    Provide exactly one of `cron` / `run_at`. Implementation is delete-and-
    recreate so APScheduler picks up the new trigger cleanly.
    """
    from datetime import datetime as _dt
    if (body.cron is None) == (body.run_at is None):
        raise HTTPException(status_code=400, detail="provide exactly one of cron / run_at")
    async with sm() as db:
        old = await db.get(ScheduleEntry, sid)
        if old is None:
            raise HTTPException(status_code=404)
        workflow_def_id = old.workflow_def_id
        parameters = old.parameters or {}
    run_at: _dt | None = None
    if body.run_at is not None:
        try:
            s = body.run_at.replace("Z", "+00:00")
            run_at = _dt.fromisoformat(s)
            if run_at.tzinfo is not None:
                run_at = run_at.astimezone(UTC).replace(tzinfo=None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"invalid run_at: {e}") from e
    sched = _sched(request)
    await sched.delete_schedule(sid)
    try:
        row = await sched.add_schedule(
            workflow_def_id=workflow_def_id,
            cron_expr=body.cron,
            run_at=run_at,
            parameters=parameters,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return _se_dict(row)


@router.post("/schedules/{sid}/enable")
async def enable_schedule(sid: str, request: Request):
    row = await _sched(request).set_enabled(sid, True)
    if row is None:
        raise HTTPException(status_code=404)
    return _se_dict(row)


@router.post("/schedules/{sid}/disable")
async def disable_schedule(sid: str, request: Request):
    row = await _sched(request).set_enabled(sid, False)
    if row is None:
        raise HTTPException(status_code=404)
    return _se_dict(row)


@router.delete("/schedules/{sid}")
async def delete_schedule(sid: str, request: Request):
    ok = await _sched(request).delete_schedule(sid)
    if not ok:
        raise HTTPException(status_code=404)
    return {"deleted": True}


# ---- runs ----
@router.get("/runs")
async def list_runs(
    limit: int = 100,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """List Run rows (each = one stage execution inside a workflow mission).

    P3 will rename `Run` → `StageExecution`; endpoint path stays the same
    for compat during the rename.
    """
    async with sm() as db:
        stmt = select(Run).order_by(Run.started_at.desc()).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
        return {"count": len(rows), "items": [_run_dict(r) for r in rows]}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as db:
        row = await db.get(Run, run_id)
        if row is None:
            raise HTTPException(status_code=404)
        outs = (await db.execute(select(Output).where(Output.run_id == run_id))).scalars().all()
        return {
            **_run_dict(row),
            "outputs": [
                {
                    "id": o.id,
                    "path": o.path,
                    "type": o.type.value if hasattr(o.type, "value") else o.type,
                    "preview": o.preview,
                    "discovered_at": iso_utc(o.discovered_at),
                }
                for o in outs
            ],
        }


@router.get("/runs/{run_id}/outputs/{output_id}/raw")
async def get_output_raw(
    run_id: str,
    output_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Serve the raw file referenced by an Output row.

    5 MB cap; UTF-8 preferred, falls back to octet-stream. P2: dropped the
    TaskDefinition-cwd sandbox check (M4 no longer exists). For workflow
    Runs, the file is inside the mission workspace by construction.
    """
    from pathlib import Path as _P

    from fastapi.responses import PlainTextResponse, Response
    async with sm() as db:
        out = await db.get(Output, output_id)
        if out is None or out.run_id != run_id:
            raise HTTPException(status_code=404, detail="output not found")
    target = _P(out.path)
    if not target.is_file():
        raise HTTPException(status_code=410, detail="output file no longer on disk")
    MAX_BYTES = 5 * 1024 * 1024
    try:
        size = target.stat().st_size
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"stat failed: {e}") from e
    if size > MAX_BYTES:
        return PlainTextResponse(
            f"[file is {size} bytes, exceeds 5 MB cap; open it on disk: {target}]",
            status_code=413,
        )
    try:
        data = target.read_bytes()
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"read failed: {e}") from e
    try:
        text = data.decode("utf-8")
        return PlainTextResponse(text)
    except UnicodeDecodeError:
        return Response(content=data, media_type="application/octet-stream")


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, request: Request):
    """User-initiated cancel: stops the underlying AUTO session and marks the
    Run as FAILED with exit_code=-2 (convention for "user cancelled").
    """
    runner = _runner(request)
    try:
        row = await runner.cancel_run(run_id)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=409, detail=msg) from e
    return _run_dict(row)


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: str, request: Request):
    runner = _runner(request)
    try:
        row = await runner.retry_run(run_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return _run_dict(row)
