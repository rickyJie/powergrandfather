"""REST endpoints for M8 workflow definitions."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.config import settings
from csm.models import Mission, Project, Run, WorkflowDefinition
from csm.models.run import RunStatus
from csm.modules.workflow.authoring import (
    clarify_workflow,
    edit_workflow,
    generate_workflow,
)
from csm.modules.workflow.loader import WorkflowLoadError
from csm.modules.workflow.rendering import PromptRenderError, render_prompt
from csm.modules.workflow.reviewer import review_workflow
from csm.modules.workflow.schema import load_workflow_spec
from csm.utils.time import now_utc_naive

router = APIRouter(prefix="/api", tags=["workflows"])


def _loader(request: Request):
    loader = getattr(request.app.state, "workflow_loader", None)
    if loader is None:
        raise HTTPException(status_code=503, detail="workflow loader not initialized")
    return loader


def _parse_parameters(yaml_content: str | None) -> list[dict[str, Any]]:
    """Parse the workflow's parameter declarations out of its YAML.

    Returned shape (frontend-friendly):
        [{name, type, required, default, description}, ...]

    Returns [] on parse failure — the workflow will still be shown but
    the launch button won't be able to prompt for params. Callers can
    fall back to legacy "launch with empty params" behavior.
    """
    if not yaml_content:
        return []
    try:
        spec = load_workflow_spec(yaml_content)
    except Exception:
        return []
    # NOTE on `required` normalization: `ParameterSpec.required` defaults
    # to True in the schema, but a param with a `default:` is effectively
    # optional at launch time — the backend/orchestrator will fall back
    # to that default. Frontend should only *force* the user to fill in
    # params that are required AND lack a usable default. Report the
    # user-facing meaning (not the raw ParameterSpec bit) so the
    # LaunchParamsModal doesn't red-flag every param.
    def _effectively_required(p) -> bool:
        return bool(p.required) and (p.default is None)
    return [
        {
            "name": p.name,
            "type": p.type,
            "required": _effectively_required(p),
            "default": p.default,
            "description": p.description,
        }
        for p in (spec.parameters or [])
    ]


@router.get("/workflows")
async def list_workflows(
    request: Request,
    include_archived: bool = False,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """List all persisted workflow definitions with their review verdicts.

    Powers the UI Workflows drawer — one row per registered workflow with
    enough info to show status pills, warn/fail chips, and offer a
    "launch mission" action.

    `include_archived`: soft-deleted rows are hidden by default. Pass
    `?include_archived=true` if you want to see them (e.g. to un-archive
    manually via DB — no UI un-archive endpoint by design).

    Each row also carries `last_run_at` + `last_status` derived from the
    workflow's most recent mission — lets the frontend show "healthy /
    ran 2 hours ago" without a second round-trip per row.
    """
    async with sm() as db:
        stmt = select(WorkflowDefinition).order_by(WorkflowDefinition.name)
        if not include_archived:
            stmt = stmt.where(WorkflowDefinition.archived_at.is_(None))
        rows = (await db.execute(stmt)).scalars().all()

        # One extra query for last-mission-per-workflow. GROUP BY on
        # started_at handles a workflow that has zero missions (row is
        # simply absent from the map — frontend renders as "never run").
        # Ordering by started_at DESC + picking [0] via subquery is
        # simpler than a per-row LEFT JOIN LATERAL (which SQLite lacks).
        last_by_wf: dict[str, tuple[datetime | None, str | None]] = {}
        if rows:
            wf_ids = [r.id for r in rows]
            max_started = (
                select(
                    Mission.workflow_def_id.label("wf_id"),
                    func.max(Mission.started_at).label("max_started"),
                )
                .where(
                    Mission.workflow_def_id.in_(wf_ids),
                    Mission.started_at.is_not(None),
                )
                .group_by(Mission.workflow_def_id)
                .subquery()
            )
            joined = (
                select(Mission.workflow_def_id, Mission.started_at, Mission.status)
                .join(
                    max_started,
                    (Mission.workflow_def_id == max_started.c.wf_id)
                    & (Mission.started_at == max_started.c.max_started),
                )
            )
            for wf_id, started_at, status in (await db.execute(joined)).all():
                last_by_wf[wf_id] = (
                    started_at,
                    status.value if hasattr(status, "value") else status,
                )

        # Resolve project_id → project name for each row so the frontend
        # can group without a second /projects fetch. Only active
        # projects show a name — an archived-then-restored edge case
        # would show the id but no name (safe fallback to heuristic).
        proj_names: dict[str, str] = {}
        proj_ids = {r.project_id for r in rows if r.project_id}
        if proj_ids:
            proj_rows = (await db.execute(
                select(Project.id, Project.name).where(Project.id.in_(proj_ids))
            )).all()
            proj_names = {pid: name for pid, name in proj_rows}
    items = []
    for r in rows:
        last_run_at, last_status = last_by_wf.get(r.id, (None, None))
        items.append({
            "id": r.id,
            "name": r.name,
            "description": r.description,
            "file_path": r.file_path,
            "project_id": r.project_id,
            "project_name": proj_names.get(r.project_id) if r.project_id else None,
            "review_status": (
                r.review_status.value
                if hasattr(r.review_status, "value")
                else r.review_status
            ),
            "review_report": r.review_report,
            "reviewed_at": (
                r.reviewed_at.isoformat() if r.reviewed_at else None
            ),
            "archived_at": r.archived_at.isoformat() if r.archived_at else None,
            "last_run_at": last_run_at.isoformat() if last_run_at else None,
            "last_status": last_status,
            # Frontend needs this to render the launch-params modal
            # and refuse the empty-params launch that used to hit the
            # required=true, default=None case with a 500.
            "parameters": _parse_parameters(r.yaml_content),
        })
    return {"count": len(items), "items": items}


@router.get("/workflows/{name}")
async def get_workflow(
    name: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Full detail for one workflow — includes `yaml_content` (for the UI
    editor) plus the review_report. Distinct from `list_workflows` which
    omits YAML for payload-size reasons.
    """
    async with sm() as db:
        row = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "file_path": row.file_path,
        "yaml_content": row.yaml_content,
        "review_status": (
            row.review_status.value
            if hasattr(row.review_status, "value")
            else row.review_status
        ),
        "review_report": row.review_report,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "parameters": _parse_parameters(row.yaml_content),
    }


class UpdateYamlBody(BaseModel):
    """Body for `PUT /api/workflows/{name}` — replace the YAML source."""
    yaml_content: str


@router.put("/workflows/{name}")
async def update_workflow_yaml(
    name: str,
    body: UpdateYamlBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Replace a workflow's YAML source in one operation.

    - Parses the new YAML (400 on schema failure)
    - Rejects if the new YAML's `name` field differs from the URL `name`
    - Writes to disk (same path as the current WorkflowDefinition)
    - Runs R9-R19 review
    - Upserts the WorkflowDefinition row
    - Returns the same shape as `GET /workflows/{name}`
    """
    from pathlib import Path

    from csm.modules.workflow.schema import WorkflowSchemaError

    async with sm() as db:
        row = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")

    try:
        spec = load_workflow_spec(body.yaml_content)
    except WorkflowSchemaError as e:
        raise HTTPException(status_code=400, detail=f"YAML schema failed: {e}") from e

    if spec.name != name:
        raise HTTPException(
            status_code=400,
            detail=(
                f"YAML's `name: {spec.name!r}` does not match URL name {name!r}. "
                "Rename via the `+ New workflow` flow instead of an in-place edit."
            ),
        )

    review = review_workflow(spec)
    review_report = review.to_dict()
    file_path = Path(row.file_path)

    try:
        file_path.write_text(body.yaml_content, encoding="utf-8")
    except OSError as e:
        raise HTTPException(
            status_code=500, detail=f"cannot write {file_path}: {e}"
        ) from e

    async with sm() as db:
        row = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
        row.description = spec.description
        row.yaml_content = body.yaml_content
        row.review_report = review_report
        row.review_status = review.status  # type: ignore[assignment]
        row.reviewed_at = now_utc_naive()
        await db.commit()
        await db.refresh(row)

    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "file_path": row.file_path,
        "yaml_content": row.yaml_content,
        "review_status": (
            row.review_status.value
            if hasattr(row.review_status, "value")
            else row.review_status
        ),
        "review_report": row.review_report,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
        "parameters": _parse_parameters(row.yaml_content),
    }


@router.delete("/workflows/{name}")
async def delete_workflow(
    name: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Delete a workflow: removes the row + the YAML file on disk.

    Missions previously launched from this workflow keep their history;
    their definition link is cleared in the same transaction and the UI
    shows them as "(deleted workflow)" without violating referential integrity.
    """
    from pathlib import Path

    async with sm() as db:
        row = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
        file_path = row.file_path
        await db.execute(
            update(Mission)
            .where(Mission.workflow_def_id == row.id)
            .values(workflow_def_id=None)
        )
        await db.delete(row)
        await db.commit()
    try:
        p = Path(file_path)
        if p.is_file():
            p.unlink()
    except OSError:
        pass  # DB deletion succeeded; disk cleanup is best-effort
    return {"deleted": True, "name": name, "file_path": file_path}


@router.post("/workflows/{name}/archive")
async def archive_workflow(
    name: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Soft-delete: hide the workflow from the default list without
    dropping the DB row or the YAML file.

    Past missions keep their `workflow_def_id` pointer intact so the
    Missions tab can still show "workflow X, mission from 2 weeks ago".
    There is intentionally NO un-archive endpoint (per the P1-B design
    decision — 纯软删); if a user changes their mind they can re-run
    the workflow reload flow to bring it back active.
    """
    async with sm() as db:
        row = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
        if row is None:
            raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
        if row.archived_at is not None:
            # Idempotent — a second archive call is not an error.
            return {"archived": True, "name": name, "archived_at": row.archived_at.isoformat()}
        row.archived_at = now_utc_naive()
        await db.commit()
        await db.refresh(row)
    return {"archived": True, "name": name, "archived_at": row.archived_at.isoformat()}


class MoveWorkflowBody(BaseModel):
    """Body for `POST /api/workflows/{name}/move` — reassign to a project.

    `project_id=null` un-assigns the workflow (falls back to the
    auto-derived heuristic bucket in the UI). A non-null id must refer
    to a non-archived Project.
    """
    project_id: str | None = None


@router.post("/workflows/{name}/move")
async def move_workflow(
    name: str,
    body: MoveWorkflowBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Reassign a workflow's project binding."""
    async with sm() as db:
        wf = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
        if wf is None:
            raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")
        if body.project_id is not None:
            proj = await db.get(Project, body.project_id)
            if proj is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"project {body.project_id!r} not found",
                )
            if proj.archived_at is not None:
                raise HTTPException(
                    status_code=400,
                    detail=f"project {body.project_id!r} is archived",
                )
        wf.project_id = body.project_id
        await db.commit()
        await db.refresh(wf)
    return {"moved": True, "name": name, "project_id": wf.project_id}


@router.post("/workflows/reload")
async def reload_workflows(request: Request) -> dict:
    """Re-scan the tasks directory for `*.workflow.yaml` and upsert each.

    Returns the count + a per-row summary (name, file_path, review_status).
    Schema-invalid YAMLs are logged at WARNING and skipped — only
    duplicate-name collisions abort the call with HTTP 400.
    """
    loader = _loader(request)
    tasks_dir = settings.tasks_dir
    if not tasks_dir.is_absolute():
        tasks_dir = settings.project_root / tasks_dir
    try:
        rows = await loader.load_directory(tasks_dir)
    except WorkflowLoadError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {
        "count": len(rows),
        "items": [
            {
                "name": r.name,
                "file_path": r.file_path,
                "review_status": (
                    r.review_status.value
                    if hasattr(r.review_status, "value")
                    else r.review_status
                ),
            }
            for r in rows
        ],
    }


class ClarifyBody(BaseModel):
    """Request body for `POST /api/workflows/clarify` (round 1).

    `requirement` is capped at 32 KiB — the string is later f-string-spliced
    into `claude -p` argv, so an unbounded payload risks argv-length blowup
    / OOM. 32 KiB is well above any realistic natural-language brief and
    still small enough to keep the subprocess argv sane.
    """
    repo_path: str
    requirement: str = Field(..., max_length=32768)
    workflow_name: str | None = None


@router.post("/workflows/clarify")
async def clarify_workflow_endpoint(body: ClarifyBody, request: Request) -> dict[str, Any]:
    """Round 1 of the two-round authoring flow.

    Spawns a short (~15-40s) claude that skims the repo + requirement,
    then emits up to 5 boundary-condition questions the user should
    answer before we commit to a YAML shape. Questions + requirement
    get stashed in an in-memory TTL cache (5 min) under a
    `clarification_id`; the client passes that id back to
    `POST /api/workflows/generate` along with the user's answers.

    Response shape:
        {
          clarification_id: str | null,
          needs_clarify: bool,
          stage_preview: str,
          questions: [...],
          duration_sec: float,
          error: str | null,
          stdout_tail: str
        }

    If `needs_clarify=false`, the frontend should skip Step 2 and go
    straight to `POST /generate` (no clarification_id needed). Any
    failure of the clarify step also degrades to `needs_clarify=false`
    so the user never gets stuck.
    """
    cache = getattr(request.app.state, "workflow_clarify_cache", None)
    if cache is None:
        raise HTTPException(status_code=503, detail="clarify cache not initialized")
    result = await clarify_workflow(
        repo_path=body.repo_path,
        requirement=body.requirement,
        workflow_name=body.workflow_name,
        cache=cache,
    )
    return {
        "clarification_id": result.clarification_id,
        "needs_clarify": result.needs_clarify,
        "stage_preview": result.stage_preview,
        "stages": result.stages,
        "questions": result.questions,
        "duration_sec": round(result.duration_sec, 2),
        "error": result.error,
        "stdout_tail": result.stdout_tail,
    }


class GenerateBody(BaseModel):
    """Request body for `POST /api/workflows/generate` (round 2).

    The endpoint spawns a one-shot `claude -p` subprocess with cwd set to
    `repo_path` and feeds it the user's natural-language `requirement`
    spliced with the authoring guide. If `clarification_id` + `answers`
    are set, those get spliced into the prompt so the agent bakes the
    boundary decisions into the YAML.

    `answers` shape: `{question_id: option_value}`. Optional per-question
    free-text can be passed via `free_text: {question_id: str}`.
    """
    repo_path: str
    requirement: str = Field(..., max_length=32768)
    workflow_name: str | None = None
    clarification_id: str | None = None
    answers: dict[str, str] | None = None
    free_text: dict[str, str] | None = None
    # User-adjusted stage skeleton from the clarify screen's preview card.
    # Each dict is `{name, kind, purpose}`. When present, the generate
    # agent is hard-locked to this decomposition — see
    # `_render_confirmed_stages` in prompt.py. Absent = agent free to
    # decide skeleton on its own (legacy / degraded path).
    confirmed_stages: list[dict[str, str]] | None = None

    @field_validator("answers", "free_text")
    @classmethod
    def _cap_value_lengths(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        """Each answer/free_text value gets spliced into the generate
        prompt, so cap each string at 32 KiB (same rationale as
        `requirement`). Rejects the whole payload if any value overflows —
        cheaper than truncating silently."""
        if v is None:
            return v
        for k, val in v.items():
            if isinstance(val, str) and len(val) > 32768:
                raise ValueError(
                    f"value for key {k!r} exceeds 32768 chars (len={len(val)})"
                )
        return v


@router.post("/workflows/generate")
async def generate_workflow_endpoint(
    body: GenerateBody,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """One-shot workflow YAML generation (round 2).

    Spawns a claude subprocess (cwd=body.repo_path), feeds it the authoring
    guide + user's requirement, waits up to 10 min, then upserts the
    generated YAML as a WorkflowDefinition and runs R9-R19 review.

    Optionally splices in the user's answers to round-1 clarification
    questions. If `clarification_id` is set but not found in cache
    (TTL expired / never issued), we silently degrade to one-shot —
    consistent with the clarify endpoint's error-fallback behavior.

    Returns the full generation result (workflow id, review report, tail
    of the agent's stdout for debugging).

    HTTP status:
    - 200: generation succeeded (workflow exists and reviewed — status may
      still be 'rejected' if reviewer flagged fail rules)
    - 500: generation aborted before a workflow could be upserted (guide
      fetch failed, claude non-zero exit, timeout, missing confirm line)

    The frontend distinguishes these by inspecting `review_status` and `error`.
    """
    # Resolve clarifications, if any, from the cache.
    clarifications_block: list[dict[str, Any]] | None = None
    if body.clarification_id:
        cache = getattr(request.app.state, "workflow_clarify_cache", None)
        entry = cache.get(body.clarification_id) if cache else None
        if entry is not None:
            answers = body.answers or {}
            free_text = body.free_text or {}
            clarifications_block = []
            for q in entry.questions:
                qid = q["id"]
                chosen_value = answers.get(qid)
                # If the client didn't send an answer, fall back to the
                # `recommended: true` option — matches the "Accept all
                # defaults & generate" one-click UX contract.
                chosen_opt = None
                if chosen_value is not None:
                    chosen_opt = next(
                        (o for o in q["options"] if o["value"] == chosen_value),
                        None,
                    )
                if chosen_opt is None:
                    chosen_opt = next(
                        (o for o in q["options"] if o.get("recommended")),
                        q["options"][0],
                    )
                clarifications_block.append({
                    "question": q["text"],
                    "answer_label": chosen_opt["label"],
                    "free_text": free_text.get(qid),
                })

    # Resolve confirmed_stages: prefer client-supplied override (user edited
    # stages on the preview card); else use the cached agent proposal as-is
    # if a clarification entry is available.
    stages_override: list[dict[str, Any]] | None = None
    if body.confirmed_stages:
        stages_override = [
            {
                "name": str(s.get("name", "")).strip(),
                "kind": str(s.get("kind", "")).strip(),
                "purpose": str(s.get("purpose", "")).strip(),
            }
            for s in body.confirmed_stages
            if isinstance(s, dict) and s.get("name")
        ] or None
    elif body.clarification_id:
        cache = getattr(request.app.state, "workflow_clarify_cache", None)
        entry = cache.get(body.clarification_id) if cache else None
        if entry is not None and entry.stages:
            stages_override = list(entry.stages)

    result = await generate_workflow(
        repo_path=body.repo_path,
        requirement=body.requirement,
        workflow_name=body.workflow_name,
        sessionmaker=sm,
        clarifications=clarifications_block,
        confirmed_stages=stages_override,
    )
    payload = {
        "workflow_id": result.workflow_id,
        "workflow_name": result.workflow_name,
        "yaml_path": result.yaml_path,
        "review_status": result.review_status,
        "review_report": result.review_report,
        "stdout_tail": result.stdout_tail,
        "duration_sec": round(result.duration_sec, 2),
        "error": result.error,
    }
    if result.workflow_id is not None:
        return payload
    raise HTTPException(status_code=500, detail=payload)


class EditWithAgentBody(BaseModel):
    """Request body for `POST /api/workflows/{name}/edit-with-agent`.

    User gives a natural-language `feedback` string describing what to
    change in the existing workflow YAML. Backend spawns claude with cwd
    set to the CSM tasks directory (no repo dependency needed — this is
    a pure YAML iteration), agent reads current YAML + splices feedback,
    writes new YAML back. Same review pipeline runs on the result.

    `feedback` capped at 32 KiB — same reason as `ClarifyBody.requirement`:
    the string is spliced into `claude -p` argv.
    """
    feedback: str = Field(..., max_length=32768)


@router.post("/workflows/{name}/edit-with-agent")
async def edit_workflow_endpoint(
    name: str,
    body: EditWithAgentBody,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Iterate on an existing workflow via natural-language feedback.

    Returns the same shape as `POST /workflows/generate` so the frontend
    can reuse its verdict card. `error` populated on failure.
    """
    result = await edit_workflow(
        workflow_name=name,
        feedback=body.feedback,
        sessionmaker=sm,
    )
    payload = {
        "workflow_id": result.workflow_id,
        "workflow_name": result.workflow_name,
        "yaml_path": result.yaml_path,
        "review_status": result.review_status,
        "review_report": result.review_report,
        "stdout_tail": result.stdout_tail,
        "duration_sec": round(result.duration_sec, 2),
        "error": result.error,
    }
    if result.workflow_id is not None:
        return payload
    raise HTTPException(status_code=500, detail=payload)


@router.post("/workflows/{name}/debug-session")
async def start_workflow_debug_session(
    name: str,
    request: Request,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict[str, Any]:
    """Spin up an INTERACTIVE claude session pre-loaded to iterate on a
    workflow YAML — for cases where the user wants multi-turn dialog
    instead of the one-shot edit-with-agent flow.

    Session shape:
    - cwd = CSM tasks dir (so agent can Read/Write the YAML directly)
    - type = INTERACTIVE (user talks to it in the Sessions UI)
    - initial_prompt = `build_debug_session_prompt(name)` — tells the
      agent to Read the YAML first, brief the user, then iterate.

    Returns the created session id; frontend routes to /sessions/<id>.
    """
    from csm.models.session import SessionType
    from csm.modules.workflow.authoring.prompt import (
        TASKS_DIR,
        build_debug_session_prompt,
    )

    # Verify workflow exists
    async with sm() as db:
        row = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")

    mgr = getattr(request.app.state, "session_manager", None)
    if mgr is None:
        raise HTTPException(status_code=503, detail="session manager not initialized")

    initial_prompt = build_debug_session_prompt(workflow_name=name)
    sess = await mgr.create_session(
        cwd=TASKS_DIR,
        type=SessionType.INTERACTIVE,
        title=f"workflow-debug:{name}",
        initial_prompt=initial_prompt,
    )
    return {
        "session_id": sess.id,
        "cwd": sess.cwd,
        "title": sess.title,
    }


class PreviewBody(BaseModel):
    """Optional params for the dry-render pass."""
    params: dict[str, Any] = {}


# Heuristic constants tuned against the test-run-1 duration samples.
# `_LAUNCHER_SEC` — bash-only prompts return within a minute.
# `_PER_OUTPUT_SEC` — writing one structured file takes claude ~10 min
#   because it "thinks aloud" while structuring sections. Bumped from
#   300 → 600 (×2) after Finding 3: the 300-sec baseline systematically
#   undershot 2-3× (test_c ruff_check 2.5×, g4 first 3.8×, g7 bootstrap 5.7×).
# `_PER_PRIMITIVE_SEC` — each validation primitive is a proxy for "did
#   claude produce something meeting this contract" — so it costs a
#   review pass. ~1 min per check.
# `_PER_SECTION_SEC` — each item in a `required_sections` list is a
#   distinct markdown section claude must think about, plan, and write.
#   Finding 3 identified this as the missing multiplicative term: stages
#   with many required sections were the worst underestimated. 2 min /
#   section (only counts `required_sections` primitive entries, not
#   every validation primitive).
# `_SAFETY_MARGIN` — real durations vary 1.5× between runs even on
#   identical prompts (empirically). We inflate to keep the 90th
#   percentile mission from tripping global_timeout.
# `_INTER_STAGE_BUFFER` — SESSION_ENDED → validation → next-stage
#   spawn takes a few extra seconds per stage transition. 30% headroom.
_LAUNCHER_SEC = 60
_PER_OUTPUT_SEC = 600
_PER_PRIMITIVE_SEC = 60
_PER_SECTION_SEC = 120
_SAFETY_MARGIN = 1.5
_INTER_STAGE_BUFFER = 1.3
_POLL_ATTEMPTS_GUESS = 5  # median iteration count for a real-world converging check
_HISTORY_MAX_SAMPLES = 10  # trailing history window used for stage-avg
_HISTORY_MIN_SAMPLES = 2   # below this we ignore history entirely
_HISTORY_WEIGHT_ANCHOR = 3  # weight = n / (n + anchor); at n=anchor weight = 0.5
_HISTORY_MAX_WEIGHT = 0.8  # never fully trust history — keep a floor for hardcoded model


def _count_required_sections(stage: Any) -> int:
    """Sum of `sections` list lengths across all `required_sections` primitives
    on this stage. Zero for stages that declare no required_sections check."""
    total = 0
    for vb in (stage.validation or []):
        for prim in (vb.primitives or []):
            if getattr(prim, "primitive", None) == "required_sections":
                total += len(getattr(prim, "sections", []) or [])
    return total


def _hardcoded_stage_raw(stage: Any) -> int:
    """Pre-safety-margin raw estimate from the hardcoded heuristic. Kept
    separate so history blending can average raw numbers before applying
    the safety multiplier exactly once."""
    if stage.kind == "claude":
        outputs_count = len(stage.outputs or [])
        if outputs_count == 0:
            return _LAUNCHER_SEC
        primitive_count = sum(
            len(vb.primitives or []) for vb in (stage.validation or [])
        )
        section_count = _count_required_sections(stage)
        return (
            _PER_OUTPUT_SEC * outputs_count
            + _PER_PRIMITIVE_SEC * primitive_count
            + _PER_SECTION_SEC * section_count
        )
    # poll
    interval = int(stage.poll_interval or 60)
    declared_timeout = int(stage.timeout) if stage.timeout is not None else None
    guess = interval * _POLL_ATTEMPTS_GUESS
    return min(declared_timeout, guess) if declared_timeout is not None else guess


def _estimate_stage_duration(
    stage: Any,
    *,
    history_avg: float | None = None,
    history_n: int = 0,
) -> tuple[int, dict]:
    """Rough per-stage wall-clock estimate in seconds.

    Deliberately over-conservative — undersizing `global_timeout` is the
    #1 first-time failure mode we see. Better to warn a user
    that a workflow is "too pessimistic" than to fail their first run.

    Heuristic per kind:
      - claude: `_LAUNCHER_SEC` if the stage produces no declared
        outputs (assumed to be a bash-only launcher/helper), else
        `_PER_OUTPUT_SEC * outputs + _PER_PRIMITIVE_SEC * primitives
        + _PER_SECTION_SEC * required_sections_count`.
      - poll: `min(stage.timeout or 3600, poll_interval * guess)`
        so a workflow that declares a tight `timeout` inherits it as the
        estimate, while one that leaves timeout open falls back to a
        guess-times-interval budget.

    When `history_avg` + `history_n>=_HISTORY_MIN_SAMPLES` are supplied
    (from `_fetch_stage_history`), a Bayesian-style blend is used:
    `weight = min(n / (n + 3), 0.8)` — more samples → more trust in
    history but never full trust. This corrects the hardcoded model's
    tendency to overshoot on tiny-payload stages (test-run-2 measured
    30-90× overshoot on trivial writes) once real data exists.

    Returns (estimate_sec, meta) where meta describes which sources
    contributed. The meta dict is echoed into the preview response so
    users can see why an estimate looks the way it does.
    """
    raw_hardcoded = _hardcoded_stage_raw(stage)
    meta: dict = {"hardcoded_raw_sec": raw_hardcoded, "history_samples": history_n}
    if (
        history_avg is not None
        and history_n >= _HISTORY_MIN_SAMPLES
        and history_avg > 0
    ):
        weight = min(history_n / (history_n + _HISTORY_WEIGHT_ANCHOR), _HISTORY_MAX_WEIGHT)
        raw = weight * history_avg + (1 - weight) * raw_hardcoded
        meta.update({"history_avg_sec": history_avg, "history_weight": round(weight, 3)})
    else:
        raw = raw_hardcoded
        meta["history_avg_sec"] = None
        meta["history_weight"] = 0.0
    return int(raw * _SAFETY_MARGIN), meta


async def _fetch_stage_history(db, wf_id: str, stage_name: str) -> tuple[float | None, int]:
    """Return (avg_seconds, sample_count) for the trailing `_HISTORY_MAX_SAMPLES`
    finished runs of this stage under this workflow. Both SUCCEEDED and FAILED
    runs count — the duration reflects real claude wall-clock either way.

    Runs where start or end timestamps are missing (mid-flight, or crashed
    before ended_at got stamped) are skipped."""
    stmt = (
        select(Run.started_at, Run.ended_at)
        .join(Mission, Mission.id == Run.mission_id)
        .where(
            Mission.workflow_def_id == wf_id,
            Run.stage_name == stage_name,
            Run.status.in_([RunStatus.SUCCEEDED, RunStatus.FAILED]),
            Run.started_at.is_not(None),
            Run.ended_at.is_not(None),
        )
        .order_by(Run.started_at.desc())
        .limit(_HISTORY_MAX_SAMPLES)
    )
    rows = (await db.execute(stmt)).all()
    durations: list[float] = []
    for started, ended in rows:
        if started is None or ended is None:
            continue
        d = (ended - started).total_seconds()
        if d > 0:
            durations.append(d)
    if not durations:
        return None, 0
    return sum(durations) / len(durations), len(durations)


@router.post("/workflows/{name}/preview")
async def preview_workflow(
    name: str,
    body: PreviewBody | None = None,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
) -> dict:
    """Dry-run everything the orchestrator does *before* it starts burning tokens.

    Reports, per stage:
      - `prompt_rendered` — the concrete prompt the AUTO session would receive
      - `prompt_render_errors` — placeholder resolution errors (unknown token,
        missing param, out-of-range stage output). Empty on success.
      - `outputs_resolved` — the workspace-relative output paths after
        placeholder substitution.
      - `validation_summary` — for claude stages, lists which primitives
        would run against which file. For poll stages, lists the check
        forms (shell / primitive / load-binding).

    Also runs the R9-R12 structural reviewer against the parsed spec so
    the caller gets the same pass/fail verdict that `load_directory`
    computes on write.

    Body is optional. When `params` is provided, `{params.X}` placeholders
    resolve against it. When absent, missing-param errors surface — useful
    for confirming which parameters are required.

    Zero side effects: no Mission row, no workspace, no session. Safe to
    hit repeatedly while iterating on a YAML.
    """
    body = body or PreviewBody()
    async with sm() as db:
        wf_row = await db.scalar(
            select(WorkflowDefinition).where(WorkflowDefinition.name == name)
        )
    if wf_row is None:
        raise HTTPException(status_code=404, detail=f"workflow {name!r} not found")

    try:
        spec = load_workflow_spec(wf_row.yaml_content)
    except Exception as e:  # schema-level parse error
        raise HTTPException(status_code=400, detail=f"YAML parse failed: {e}") from e

    # Structural review (R9-R12) — same reviewer the loader uses.
    review = review_workflow(spec)

    # Merge caller params with declared defaults — same normalisation
    # the orchestrator applies at launch time. Users iterating on the
    # YAML shouldn't need to redundantly supply values that already
    # have a `default:` on the parameter spec.
    effective_params: dict[str, Any] = dict(body.params)
    for p in spec.parameters:
        if p.name not in effective_params and p.default is not None:
            effective_params[p.name] = p.default

    # Placeholder-render every stage's prompt against a synthetic workspace
    # so the caller sees the exact string claude would receive. Cross-stage
    # outputs come from **spec** (not DB) — this is a dry-run, no Runs.
    fake_workspace = f"<preview-workspace>/{name}"
    fake_mission_id = "<preview-mission-id>"
    stage_outputs: dict[str, list[str]] = {}
    stages_report: list[dict[str, Any]] = []

    for stage in spec.stages:
        # Resolve outputs first — the outputs of THIS stage become
        # references for LATER stages' prompts.
        resolved_outputs: list[str] = []
        outputs_errors: list[str] = []
        for out_template in stage.outputs or []:
            try:
                path = render_prompt(
                    out_template,
                    params=dict(effective_params),
                    workspace_path=fake_workspace,
                    mission_id=fake_mission_id,
                    workflow_name=spec.name,
                    stage_outputs=stage_outputs,
                )
                resolved_outputs.append(path)
            except PromptRenderError as e:
                outputs_errors.append(f"{out_template}: {e}")

        prompt_rendered: str | None = None
        prompt_errors: list[str] = []
        if stage.kind == "claude" and stage.prompt is not None:
            try:
                prompt_rendered = render_prompt(
                    stage.prompt,
                    params=dict(effective_params),
                    workspace_path=fake_workspace,
                    mission_id=fake_mission_id,
                    workflow_name=spec.name,
                    stage_outputs=stage_outputs,
                )
            except PromptRenderError as e:
                prompt_errors = [str(e)]

        # Validation summary — describe the shape, not run.
        validation_summary: list[dict[str, Any]] = []
        if stage.kind == "claude":
            for vb in stage.validation or []:
                primitives = [
                    getattr(p, "primitive", type(p).__name__)
                    for p in (vb.primitives or [])
                ]
                validation_summary.append({
                    "file": vb.file,
                    "primitives": primitives,
                })
        elif stage.kind == "poll":
            for cb in stage.check or []:
                form = (
                    "shell-exec" if cb.command is not None
                    else "load-binding" if cb.bind_as is not None
                    else "primitive" if cb.primitives is not None
                    else "unknown"
                )
                validation_summary.append({
                    "file": cb.file,
                    "form": form,
                    "primitives": [
                        getattr(p, "primitive", type(p).__name__)
                        for p in (cb.primitives or [])
                    ] if cb.primitives else None,
                })

        # Blend hardcoded heuristic with actual historical Run durations
        # for this stage under this workflow. First-time-run has no history
        # so the heuristic drives; established stages converge to observed
        # duration as samples accumulate.
        async with sm() as _hist_db:
            hist_avg, hist_n = await _fetch_stage_history(_hist_db, wf_row.id, stage.name)
        estimated_sec, est_meta = _estimate_stage_duration(
            stage, history_avg=hist_avg, history_n=hist_n
        )
        stages_report.append({
            "name": stage.name,
            "kind": stage.kind,
            "prompt_rendered": prompt_rendered,
            "prompt_render_errors": prompt_errors,
            "outputs_resolved": resolved_outputs,
            "outputs_render_errors": outputs_errors,
            "validation_summary": validation_summary,
            "estimated_duration_sec": estimated_sec,
            "estimate_meta": est_meta,
        })

        # Register this stage's outputs so subsequent stages can reference them.
        stage_outputs[stage.name] = resolved_outputs

    # Roll up per-stage estimates → suggested global_timeout. Adds
    # inter-stage buffer for SESSION_ENDED → validation → next-spawn
    # bookkeeping between stages.
    total_stage_estimate = sum(s["estimated_duration_sec"] for s in stages_report)
    suggested_global_timeout = int(total_stage_estimate * _INTER_STAGE_BUFFER)
    declared_global_timeout = int(spec.global_timeout)
    # Warn only when the declared budget is *materially* below the
    # suggestion — a 5% shortfall is within heuristic noise; a 30%
    # shortfall is a real problem worth surfacing.
    budget_reasonable = declared_global_timeout >= int(suggested_global_timeout * 0.7)

    return {
        "workflow_name": spec.name,
        "params_supplied": dict(body.params),
        "params_effective": dict(effective_params),
        "required_params": [
            p.name for p in spec.parameters if p.required and p.default is None
        ],
        "structural_review": review.to_dict(),
        "stages": stages_report,
        "budget": {
            "declared_global_timeout_sec": declared_global_timeout,
            "suggested_global_timeout_sec": suggested_global_timeout,
            "total_stage_estimate_sec": total_stage_estimate,
            "budget_reasonable": budget_reasonable,
            "note": (
                "estimated per-stage durations are heuristic. Real runs "
                "vary by up to 2× — treat this as a lower bound, not a plan."
            ),
        },
    }
