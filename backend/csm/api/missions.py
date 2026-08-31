"""REST endpoints for M8 Missions (workflow instances).

Minimal surface: launch / list / detail / cancel. Retry-from-stage is
deferred until the frontend needs it — the orchestrator already exposes
the method, so wiring is trivial when the UI story is designed.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.models import Mission
from csm.models.run import Run, RunStatus
from csm.models.workflow_definition import WorkflowDefinition

router = APIRouter(prefix="/api/missions", tags=["missions"])


def _orch(request: Request):
    orch = getattr(request.app.state, "orchestrator", None)
    if orch is None:
        raise HTTPException(status_code=503, detail="orchestrator not initialized")
    return orch


class LaunchBody(BaseModel):
    workflow_name: str
    params: dict[str, Any] = {}


def _mission_dict(
    m: Mission,
    stages_completed: int = 0,
    stages_total: int | None = None,
) -> dict[str, Any]:
    return {
        "id": m.id,
        "workflow_def_id": m.workflow_def_id,
        "status": m.status.value if hasattr(m.status, "value") else m.status,
        "current_stage": m.current_stage,
        "parameters": dict(m.parameters or {}),
        "workspace_path": m.workspace_path,
        "started_at": m.started_at.isoformat() if m.started_at else None,
        "ended_at": m.ended_at.isoformat() if m.ended_at else None,
        "failure_reason": m.failure_reason,
        "audit_log": list(m.audit_log or []),
        # Progress signals for the frontend (local:380d5b52). `stages_total`
        # is the workflow-def stage count (denominator for a proper bar);
        # `stages_completed` counts stage_execution rows in a terminal state
        # (SUCCEEDED / FAILED). Both default to safe values if the caller
        # skips enrichment.
        "stages_completed": stages_completed,
        "stages_total": stages_total,
    }


async def _batch_mission_progress(
    sm: async_sessionmaker,
    missions: list[Mission],
) -> tuple[dict[str, int], dict[str, int | None]]:
    """Two batch queries → returns (completed_by_mid, total_by_mid).

    - completed_by_mid[mission_id] = # of stage_execution rows in a terminal
      status (SUCCEEDED / FAILED). Uses one GROUP BY query keyed on
      mission_id, so the whole list is one round trip.
    - total_by_mid[mission_id] = len(workflow_def.compiled_rules['stages'])
      when compiled_rules is present, else None. One IN () query.
    """
    if not missions:
        return {}, {}
    mission_ids = [m.id for m in missions]
    wf_ids = list({m.workflow_def_id for m in missions if m.workflow_def_id})
    async with sm() as db:
        completed_rows = (
            await db.execute(
                select(Run.mission_id, func.count(Run.id))
                .where(
                    Run.mission_id.in_(mission_ids),
                    Run.status.in_((RunStatus.SUCCEEDED, RunStatus.FAILED)),
                )
                .group_by(Run.mission_id)
            )
        ).all()
        wf_rows: list[tuple[str, dict | None]] = []
        if wf_ids:
            wf_rows = (
                await db.execute(
                    select(WorkflowDefinition.id, WorkflowDefinition.compiled_rules)
                    .where(WorkflowDefinition.id.in_(wf_ids))
                )
            ).all()
    completed_by_mid: dict[str, int] = {mid: int(cnt) for mid, cnt in completed_rows if mid}
    stages_by_wf: dict[str, int | None] = {}
    for wf_id, rules in wf_rows:
        stages = None
        if isinstance(rules, dict):
            s = rules.get("stages")
            if isinstance(s, dict):
                stages = len(s)
        stages_by_wf[wf_id] = stages
    total_by_mid: dict[str, int | None] = {
        m.id: stages_by_wf.get(m.workflow_def_id) for m in missions
    }
    return completed_by_mid, total_by_mid


@router.post("/launch")
async def launch_mission(body: LaunchBody, request: Request) -> dict:
    """Create a running Mission from the named workflow.

    Errors surface as 404 (unknown workflow) or 400 (bad params /
    illegal state — e.g. workflow review not passed).
    """
    orch = _orch(request)
    try:
        mission = await orch.launch_mission(body.workflow_name, dict(body.params))
    except ValueError as e:
        # Distinguish "not found" from other validation errors by keyword.
        # Orchestrator wraps the not-found case as "unknown workflow ...".
        msg = str(e)
        code = 404 if msg.startswith("unknown workflow") else 400
        raise HTTPException(status_code=code, detail=msg) from e
    return _mission_dict(mission)


@router.get("")
async def list_missions(
    request: Request,
    limit: int = 50,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    async with sm() as db:
        rows = (
            await db.execute(
                select(Mission).order_by(Mission.started_at.desc()).limit(max(1, min(limit, 500)))
            )
        ).scalars().all()
    completed_by_mid, total_by_mid = await _batch_mission_progress(sm, list(rows))
    return {
        "items": [
            _mission_dict(
                m,
                stages_completed=completed_by_mid.get(m.id, 0),
                stages_total=total_by_mid.get(m.id),
            )
            for m in rows
        ]
    }


@router.get("/{mission_id}")
async def get_mission(
    mission_id: str,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    orch = _orch(request)
    mission = await orch.get_mission(mission_id)
    if mission is None:
        raise HTTPException(status_code=404, detail="mission not found")
    completed_by_mid, total_by_mid = await _batch_mission_progress(sm, [mission])
    return _mission_dict(
        mission,
        stages_completed=completed_by_mid.get(mission.id, 0),
        stages_total=total_by_mid.get(mission.id),
    )


@router.post("/prune-terminal")
async def prune_terminal_missions(
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Delete every mission in a terminal state (SUCCEEDED / FAILED /
    CANCELLED). Runs that referenced these missions are kept, with
    mission_id NULLed; per-mission workspace directories on disk are
    NOT touched (the artifacts stay for post-mortem).
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import update as sa_update

    from csm.models import Run
    from csm.models.mission import MissionStatus
    async with sm() as db:
        rows = (
            await db.execute(
                select(Mission).where(
                    Mission.status.in_([
                        MissionStatus.SUCCEEDED,
                        MissionStatus.FAILED,
                        MissionStatus.CANCELLED,
                    ])
                )
            )
        ).scalars().all()
        ids = [m.id for m in rows]
        if ids:
            await db.execute(
                sa_update(Run).where(Run.mission_id.in_(ids)).values(mission_id=None)
            )
            await db.execute(sa_delete(Mission).where(Mission.id.in_(ids)))
            await db.commit()
    return {"deleted_count": len(ids), "deleted_ids": ids}


@router.post("/{mission_id}/cancel")
async def cancel_mission(mission_id: str, request: Request) -> dict:
    orch = _orch(request)
    try:
        mission = await orch.cancel_mission(mission_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        # Illegal state transition (already terminal) → 409.
        from csm.modules.workflow.orchestrator import InvalidMissionStateTransition
        if isinstance(e, InvalidMissionStateTransition):
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise
    return _mission_dict(mission)


@router.post("/{mission_id}/retry")
async def retry_mission(
    mission_id: str,
    stage: str,
    request: Request,
    mode: str = "rerun",
) -> dict:
    """Move a `failed` Mission back to `running`, rewinding to `stage`.

    Query params:
    - `stage` — must be a stage declared on the workflow.
    - `mode` (default `rerun`):
      - `rerun` — spawn a fresh AUTO session / poll loop for the stage.
        Fits "the stage crashed, try again".
      - `revalidate` — skip spawning; re-run validation against the
        current workspace files. If pass, advance to next stage; if
        fail, re-mark the Mission failed with the new reason. Fits
        "I fixed the file manually, please re-check".
    """
    if mode not in ("rerun", "revalidate"):
        raise HTTPException(
            status_code=400,
            detail=f"invalid mode {mode!r}; expected 'rerun' or 'revalidate'",
        )
    orch = _orch(request)
    try:
        mission = await orch.retry_from_stage(mission_id, stage, mode=mode)
    except ValueError as e:
        # "unknown mission" / "stage not declared" both map to 404.
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        from csm.modules.workflow.orchestrator import InvalidMissionStateTransition
        if isinstance(e, InvalidMissionStateTransition):
            raise HTTPException(status_code=409, detail=str(e)) from e
        raise
    return _mission_dict(mission)
