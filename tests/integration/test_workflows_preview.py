"""Phase 2 G1-G3: preview endpoint integration tests.

Goal: prove `POST /api/workflows/{name}/preview` catches the exact user
errors that Phase 1 exposed at real-launch time, at zero token cost.
Each test seeds a WorkflowDefinition directly (bypasses YAML loader) so
the preview surface is exercised in isolation from disk state.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest
from csm.models import Base, Mission, Run, WorkflowDefinition, WorkflowReviewStatus
from csm.models.mission import MissionStatus
from csm.models.run import RunStatus
from csm.modules.workflow.engine import compile_workflow
from csm.modules.workflow.schema import load_workflow_spec
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def app_client(tmp_path, monkeypatch):
    """FastAPI app with an in-memory DB. The app itself is what routes
    /api/workflows/{name}/preview — mount and hit via httpx."""
    # Isolated SQLite for this test module.
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    # Point the app's get_sessionmaker at our test session.
    from csm import db as csm_db
    monkeypatch.setattr(csm_db, "_engine", engine)
    monkeypatch.setattr(csm_db, "_sessionmaker", sm)

    # Import the router only; a full csm.main lifespan pulls in the
    # whole subsystem stack we don't need for preview.
    from csm.api.workflows import router
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router)
    # D4 migration: handlers now resolve sessionmaker via Depends(get_db_sessionmaker),
    # which reads request.app.state.sessionmaker.
    app.state.sessionmaker = sm

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sm

    await engine.dispose()
    os.unlink(db_path)


async def _seed_wf(sm, yaml_content: str) -> WorkflowDefinition:
    spec = load_workflow_spec(yaml_content)
    compiled = compile_workflow(spec)
    async with sm() as s:
        wf = WorkflowDefinition(
            name=spec.name,
            description=spec.description,
            file_path=f"/tmp/{spec.name}.workflow.yaml",
            yaml_content=yaml_content,
            compiled_rules=compiled,
            review_status=WorkflowReviewStatus.PASSED,
            reviewed_at=datetime.utcnow(),
        )
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        return wf


_G1_YAML = """\
name: g1_placeholder_typo
description: Preview catches unknown placeholder before launch.
parameters:
  - name: topic
    type: string
    required: true
stages:
  - name: only_stage
    kind: claude
    prompt: |
      Do work on {params.topic} — should also mention {cluster}
    outputs:
      - "{ws}/out.md"
"""


@pytest.mark.asyncio
async def test_g1_preview_flags_unknown_placeholder(app_client):
    """Bare `{cluster}` in a prompt → preview reports it BEFORE launch."""
    client, sm = app_client
    await _seed_wf(sm, _G1_YAML)

    r = await client.post(
        "/api/workflows/g1_placeholder_typo/preview",
        json={"params": {"topic": "audit"}},
    )
    assert r.status_code == 200
    body = r.json()
    stage = body["stages"][0]
    assert stage["prompt_render_errors"], "expected preview to flag {cluster}"
    err = stage["prompt_render_errors"][0]
    assert "{cluster}" in err
    # The error must point the user at the escape route so they can fix it themselves.
    assert "{{cluster}}" in err


_G2_YAML = """\
name: g2_missing_param
description: Preview surfaces missing-param errors.
parameters:
  - name: topic
    type: string
    required: true
  - name: baseline
    type: string
    default: "sota_v1"
stages:
  - name: only_stage
    kind: claude
    prompt: |
      Use {params.topic} vs {params.baseline} vs {params.uncovered}
    outputs:
      - "{ws}/out.md"
"""


@pytest.mark.asyncio
async def test_g2_preview_reflects_defaults_and_missing_params(app_client):
    """Defaults populated + genuinely missing params called out."""
    client, sm = app_client
    await _seed_wf(sm, _G2_YAML)

    r = await client.post(
        "/api/workflows/g2_missing_param/preview",
        json={"params": {"topic": "signal review"}},
    )
    body = r.json()
    # `baseline` had a default so it should NOT appear in required_params
    # and should be reflected in effective params.
    assert "baseline" not in body["required_params"]
    assert body["params_effective"]["baseline"] == "sota_v1"
    # `uncovered` was NEVER declared in parameters — it becomes an
    # unknown-param error at render time.
    stage = body["stages"][0]
    assert stage["prompt_render_errors"]
    err = stage["prompt_render_errors"][0]
    assert "uncovered" in err
    assert "supplied params" in err  # message must list what IS available


_G3_YAML = """\
name: g3_cross_stage_missing
description: Cross-stage reference to a stage that doesn't exist yet.
parameters: []
stages:
  - name: first
    kind: claude
    prompt: "Do first"
    outputs:
      - "{ws}/first.md"
  - name: second
    kind: claude
    prompt: "Read {stages.no_such_stage.outputs[0]} and expand"
    outputs:
      - "{ws}/second.md"
"""


@pytest.mark.asyncio
async def test_g3_preview_catches_bad_stage_reference(app_client):
    """`{stages.no_such_stage.outputs[0]}` → preview error, not launch."""
    client, sm = app_client
    await _seed_wf(sm, _G3_YAML)

    r = await client.post("/api/workflows/g3_cross_stage_missing/preview", json={})
    body = r.json()
    second = body["stages"][1]
    assert second["prompt_render_errors"]
    err = second["prompt_render_errors"][0]
    assert "no_such_stage" in err
    # Error must list what stages DO have outputs so the user can pick.
    assert "known stages" in err


@pytest.mark.asyncio
async def test_g_preview_404_for_unknown_workflow(app_client):
    """Sanity: preview returns 404, not 500, when workflow doesn't exist."""
    client, _ = app_client
    r = await client.post("/api/workflows/never_seeded/preview", json={})
    assert r.status_code == 404


# ---- Finding-3 regression: estimator must weight required_sections ----
_SECTION_HEAVY_YAML = """\
name: section_heavy
description: One claude stage writing a 6-section markdown output.
parameters: []
global_timeout: 3600
stages:
  - name: write_report
    kind: claude
    prompt: "Write a structured report"
    outputs:
      - "{ws}/report.md"
    validation:
      - file: "{ws}/report.md"
        primitives:
          - required_sections:
              - "## Summary"
              - "## Context"
              - "## Analysis"
              - "## Findings"
              - "## Recommendations"
              - "## Next Steps"
"""

_SECTION_LIGHT_YAML = """\
name: section_light
description: One claude stage with a single-section markdown output.
parameters: []
global_timeout: 3600
stages:
  - name: write_report
    kind: claude
    prompt: "Write a short note"
    outputs:
      - "{ws}/note.md"
    validation:
      - file: "{ws}/note.md"
        primitives:
          - required_sections:
              - "## Summary"
"""


@pytest.mark.asyncio
async def test_estimator_weights_required_sections(app_client):
    """Finding-3 (g): a stage with 6 required sections must estimate strictly
    longer than one with a single section, because the missing multiplicative
    term was the root cause of the 3-5× undershoots we saw in test-run-1."""
    client, sm = app_client
    await _seed_wf(sm, _SECTION_HEAVY_YAML)
    await _seed_wf(sm, _SECTION_LIGHT_YAML)

    heavy = (await client.post("/api/workflows/section_heavy/preview", json={})).json()
    light = (await client.post("/api/workflows/section_light/preview", json={})).json()

    heavy_est = heavy["stages"][0]["estimated_duration_sec"]
    light_est = light["stages"][0]["estimated_duration_sec"]
    # 5 extra sections × 120s × 1.5 safety = 900s minimum delta.
    assert heavy_est - light_est >= 900, (heavy_est, light_est)

    # And the bumped _PER_OUTPUT_SEC (300→600) means even the "light"
    # single-output stage should now clear ~15 minutes with safety margin.
    assert light_est >= 900, light_est


@pytest.mark.asyncio
async def test_estimator_bumped_baseline(app_client):
    """Regression pin for Finding-3: baseline per-output must be at 600, not 300.

    We assert via the estimator constant + the total: a bare 1-output, no-primitive
    stage should estimate ≥ 600s × 1.5 safety = 900s. Reading it back through the
    preview endpoint keeps this pinned even if the constant moves modules later."""
    client, sm = app_client
    yaml_content = """\
name: bare_output
description: Bare stage — 1 output, no validation.
parameters: []
global_timeout: 3600
stages:
  - name: only
    kind: claude
    prompt: "Do it"
    outputs:
      - "{ws}/x.md"
"""
    await _seed_wf(sm, yaml_content)
    body = (await client.post("/api/workflows/bare_output/preview", json={})).json()
    est = body["stages"][0]["estimated_duration_sec"]
    assert est >= 900, f"baseline estimator must be at least 900s post-Finding-3, got {est}"


# ---- TR3 F3-followup: estimator blends historical Run durations ----

_HISTORY_YAML = """\
name: history_wf
description: One claude stage — we'll fabricate history and check the blend.
parameters: []
global_timeout: 3600
stages:
  - name: only
    kind: claude
    prompt: "Do it"
    outputs:
      - "{ws}/x.md"
"""


async def _seed_run_with_duration(sm, wf_id: str, stage_name: str, seconds: int, status=RunStatus.SUCCEEDED):
    """Seed a Mission + Run row with a known duration for history-blend tests."""
    async with sm() as s:
        m = Mission(
            workflow_def_id=wf_id,
            parameters={},
            workspace_path="/tmp/histwf",
            status=MissionStatus.SUCCEEDED,
            current_stage=stage_name,
            started_at=datetime.utcnow() - timedelta(hours=1),
        )
        s.add(m)
        await s.commit()
        await s.refresh(m)
        started = datetime.utcnow() - timedelta(seconds=seconds + 10)
        ended = started + timedelta(seconds=seconds)
        r = Run(
            session_id=None,
            status=status,
            parameters={},
            mission_id=m.id,
            stage_name=stage_name,
            started_at=started,
            ended_at=ended,
        )
        s.add(r)
        await s.commit()


@pytest.mark.asyncio
async def test_estimator_history_blend_pulls_estimate_down_after_fast_runs(app_client):
    """TR3-F3 followup: after a workflow has run many times fast, the estimator
    should trust that history over the pessimistic hardcoded heuristic — otherwise
    users chronically over-budget their `global_timeout` for well-characterised
    workflows. Hardcoded baseline for a 1-output claude stage is 900s; historical
    runs at 60s should blend the estimate to somewhere in between (well below 900)."""
    client, sm = app_client
    wf = await _seed_wf(sm, _HISTORY_YAML)
    # 5 fast runs — enough weight to matter.
    for _ in range(5):
        await _seed_run_with_duration(sm, wf.id, "only", seconds=60)

    body = (await client.post("/api/workflows/history_wf/preview", json={})).json()
    stage = body["stages"][0]
    meta = stage["estimate_meta"]
    assert meta["history_samples"] == 5
    assert meta["history_avg_sec"] == pytest.approx(60.0)
    # weight = 5 / (5+3) = 0.625; blended raw ≈ 0.625*60 + 0.375*600 = 262.5;
    # × safety 1.5 ≈ 393. Assert strictly under the 900 hardcoded baseline.
    assert stage["estimated_duration_sec"] < 900
    assert stage["estimated_duration_sec"] > 100  # sanity: still applies safety margin


@pytest.mark.asyncio
async def test_estimator_history_ignored_below_min_samples(app_client):
    """A single sample is too noisy to trust — heuristic must dominate."""
    client, sm = app_client
    wf = await _seed_wf(sm, _HISTORY_YAML)
    await _seed_run_with_duration(sm, wf.id, "only", seconds=60)

    body = (await client.post("/api/workflows/history_wf/preview", json={})).json()
    stage = body["stages"][0]
    assert stage["estimate_meta"]["history_samples"] == 1
    assert stage["estimate_meta"]["history_weight"] == 0.0
    assert stage["estimated_duration_sec"] >= 900  # hardcoded baseline still applies
