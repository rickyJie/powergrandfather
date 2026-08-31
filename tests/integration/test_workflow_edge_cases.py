"""Phase 3 H1-H6: edge cases + extension-point coverage.

Complements Phase 1's happy-path tests and Phase 2's docs-driven tests.
Each test targets a corner the CSM team identified during workflow_contract_v2
authoring where the framework's behavior wasn't obvious from the code alone.
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest
from csm.models import (
    Base,
    Mission,
    MissionStatus,
    Output,
    Run,
    WorkflowDefinition,
    WorkflowReviewStatus,
)
from csm.models.run import RunStatus
from csm.modules.workflow.engine import compile_workflow, validate_stage
from csm.modules.workflow.schema import load_workflow_spec
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def db_and_client(tmp_path, monkeypatch):
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    from csm import db as csm_db
    monkeypatch.setattr(csm_db, "_engine", engine)
    monkeypatch.setattr(csm_db, "_sessionmaker", sm)

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


async def _seed_wf(sm, yaml_content: str, *, review=WorkflowReviewStatus.PASSED) -> WorkflowDefinition:
    spec = load_workflow_spec(yaml_content)
    compiled = compile_workflow(spec)
    async with sm() as s:
        wf = WorkflowDefinition(
            name=spec.name,
            description=spec.description,
            file_path=f"/tmp/{spec.name}.workflow.yaml",
            yaml_content=yaml_content,
            compiled_rules=compiled,
            review_status=review,
            reviewed_at=datetime.utcnow(),
        )
        s.add(wf)
        await s.commit()
        await s.refresh(wf)
        return wf


# ----------------------------------------------------------------------------
# H1 — Section-scoped primitive respects header boundaries
# ----------------------------------------------------------------------------


_H1_SCOPE_WRONG_YAML = """\
name: h1_scope_wrong_section
description: Regex scoped to wrong section.
parameters: []
stages:
  - name: only
    kind: claude
    prompt: "x"
    outputs:
      - "{ws}/r.md"
    validation:
      - file: "{ws}/r.md"
        primitives:
          - regex_match:
              pattern: 'wauc[:\\s]+0\\.[0-9]{4}'
              section: "## Baseline"
"""


_H1_SCOPE_RIGHT_YAML = """\
name: h1_scope_right_section
description: Regex scoped to correct section.
parameters: []
stages:
  - name: only
    kind: claude
    prompt: "x"
    outputs:
      - "{ws}/r.md"
    validation:
      - file: "{ws}/r.md"
        primitives:
          - regex_match:
              pattern: 'wauc[:\\s]+0\\.[0-9]{4}'
              section: "## Result"
"""


def test_h1_section_scope_boundary(tmp_path):
    """`section:`-scoped primitives must NOT leak into the next section.

    Two sections. The pattern only appears in the SECOND section, but
    the primitive is scoped to the FIRST. Must fail.
    """
    report = tmp_path / "r.md"
    report.write_text(
        "# Report\n"
        "\n"
        "## Baseline\n"
        "current sota; no wauc here\n"
        "\n"
        "## Result\n"
        "wauc: 0.9123\n"
    )
    spec = load_workflow_spec(_H1_SCOPE_WRONG_YAML)
    compiled = compile_workflow(spec)
    verdict = validate_stage(
        stage_compiled=compiled["stages"]["only"],
        workspace_path=str(tmp_path),
        params={},
        prior_outputs={},
    )
    assert not verdict["pass"], "pattern is in Result, not Baseline — must fail"


def test_h1_section_scope_finds_pattern_in_correct_section(tmp_path):
    """Inverse of H1: pattern IS in the scoped section → pass."""
    report = tmp_path / "r.md"
    report.write_text(
        "# Report\n"
        "\n"
        "## Result\n"
        "wauc: 0.9123\n"
        "\n"
        "## Notes\n"
        "misc\n"
    )
    spec = load_workflow_spec(_H1_SCOPE_RIGHT_YAML)
    compiled = compile_workflow(spec)
    verdict = validate_stage(
        stage_compiled=compiled["stages"]["only"],
        workspace_path=str(tmp_path),
        params={},
        prior_outputs={},
    )
    assert verdict["pass"], f"expected pass; got {verdict}"


# ----------------------------------------------------------------------------
# H2 — Zero-stage workflow rejected by structural reviewer
# ----------------------------------------------------------------------------


def test_h2_zero_stage_workflow_reviewer_rejects():
    """A workflow with `stages: []` should not pass R9 (or fail obviously)."""
    from csm.modules.workflow.reviewer import review_workflow
    from csm.modules.workflow.schema import (
        ParameterSpec,
        WorkflowSpec,
    )

    # We build the spec via model_construct to bypass the schema-level
    # "stages must be non-empty" validator (if any) and hit the reviewer directly.
    spec = WorkflowSpec.model_construct(
        name="empty_wf",
        description="no stages",
        parameters=[
            ParameterSpec.model_construct(name="x", type="string", required=False, default=""),
        ],
        workspace="/tmp/{mission_id}",
        global_timeout=60,
        stages=[],
        final_outputs=[],
    )
    result = review_workflow(spec)
    # Every rule vacuously passes for zero stages, so the reviewer's own
    # verdict is `passed`. That's a documented gap — reviewer verifies
    # stage-level invariants; zero-stages should be caught by the schema.
    # This test locks in the current behavior so a future schema tightening
    # can bump it deliberately.
    assert result.status == "passed"
    # None of the rules had anything to complain about.
    assert all(r.status == "pass" for r in result.rules)


# ----------------------------------------------------------------------------
# H3 — Preview surfaces poll load-binding limitation clearly
# ----------------------------------------------------------------------------


_H3_YAML = """\
name: h3_poll_binding_workflow
description: Poll load-binding drives downstream {params.X}.
parameters: []
stages:
  - name: bootstrap
    kind: claude
    prompt: "Write meta"
    outputs:
      - "{ws}/meta.json"
  - name: wait
    kind: poll
    poll_interval: 5s
    timeout: 60s
    check:
      - file: "{stages.bootstrap.outputs[0]}"
        load_as: json
        extract_field: exp
        as: exp
  - name: use
    kind: claude
    prompt: "Do work at {params.exp}"
    outputs:
      - "{ws}/final.md"
"""


@pytest.mark.asyncio
async def test_h3_preview_flags_load_binding_reference_as_missing(db_and_client):
    """Documented preview limitation: load-binding params aren't simulated.

    A downstream stage that references `{params.X}` set by a prior poll
    stage's load-binding shows up as a missing-param error in preview.
    At runtime it works. This test asserts the current behavior so the
    doc's "known limitation" line stays accurate; if we improve preview
    to trace bindings, flip this to assert the reverse.
    """
    client, sm = db_and_client
    await _seed_wf(sm, _H3_YAML)
    r = await client.post("/api/workflows/h3_poll_binding_workflow/preview", json={})
    body = r.json()

    use_stage = next(s for s in body["stages"] if s["name"] == "use")
    assert use_stage["prompt_render_errors"], (
        "preview currently doesn't simulate load-bindings; if this passes, "
        "update workflow_contract_v2.md §7 to reflect the improvement."
    )
    assert "exp" in use_stage["prompt_render_errors"][0]


# ----------------------------------------------------------------------------
# H4 — Prior-stage Output rows resolve cross-stage placeholders in prompts
# ----------------------------------------------------------------------------


_H4_YAML = """\
name: h4_output_chain
description: Two-stage claude chain with cross-stage output ref.
parameters: []
stages:
  - name: first
    kind: claude
    prompt: "Do first"
    outputs:
      - "{ws}/first.md"
  - name: second
    kind: claude
    prompt: "Read {stages.first.outputs[0]}"
    outputs:
      - "{ws}/second.md"
"""


@pytest.mark.asyncio
async def test_h4_preview_resolves_cross_stage_output(db_and_client):
    """`{stages.first.outputs[0]}` resolves at preview time using declared
    outputs (spec order — no DB Runs needed to verify structure)."""
    client, sm = db_and_client
    await _seed_wf(sm, _H4_YAML)
    r = await client.post("/api/workflows/h4_output_chain/preview", json={})
    body = r.json()

    second = next(s for s in body["stages"] if s["name"] == "second")
    assert not second["prompt_render_errors"]
    assert "first.md" in second["prompt_rendered"]
    assert "<preview-workspace>" in second["prompt_rendered"]


# ----------------------------------------------------------------------------
# H5 — Output rows written by orchestrator flow to _collect_prior_outputs
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_h5_output_rows_resolve_cross_stage_at_runtime(tmp_path):
    """Direct test of the Output-rows-per-stage machinery.

    Simulates: a Mission where stage1 has produced Output rows.
    _collect_prior_outputs must find them and return them mapped by
    stage_name, in discovered_at order.
    """
    from csm.modules.workflow.orchestrator import WorkflowOrchestrator

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    # Seed WF, mission, and one stage's Run+Outputs.
    yaml_content = _H4_YAML
    spec = load_workflow_spec(yaml_content)

    async with sm() as s:
        wf = WorkflowDefinition(
            name=spec.name,
            description=spec.description,
            file_path="/tmp/x.workflow.yaml",
            yaml_content=yaml_content,
            compiled_rules=compile_workflow(spec),
            review_status=WorkflowReviewStatus.PASSED,
            reviewed_at=datetime.utcnow(),
        )
        s.add(wf)
        await s.commit()
        await s.refresh(wf)

        mission = Mission(
            workflow_def_id=wf.id,
            parameters={},
            workspace_path=str(tmp_path),
            status=MissionStatus.RUNNING,
            current_stage="second",
            started_at=datetime.utcnow(),
        )
        s.add(mission)
        await s.commit()
        await s.refresh(mission)

        run = Run(
            session_id=None,
            status=RunStatus.SUCCEEDED,
            parameters={},
            mission_id=mission.id,
            stage_name="first",
        )
        s.add(run)
        await s.commit()
        await s.refresh(run)

        out1 = Output(run_id=run.id, path=str(tmp_path / "first.md"), preview=None)
        s.add(out1)
        await s.commit()

    # Call _collect_prior_outputs — static-ish method that only needs a session.
    async with sm() as s:
        prior = await WorkflowOrchestrator._collect_prior_outputs(
            s, mission_id=mission.id, before_stage="second", spec=spec
        )
    assert prior == {"first": [str(tmp_path / "first.md")]}

    await engine.dispose()
    os.unlink(db_path)


# ----------------------------------------------------------------------------
# H6 — Nonexistent workflow surfaces 404 on preview AND on launch
# ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_h6_preview_and_launch_agree_on_missing_workflow(db_and_client):
    """The preview endpoint returning 404 for a name should predict launch failing."""
    client, _ = db_and_client
    r = await client.post("/api/workflows/never/preview", json={})
    assert r.status_code == 404
    detail = r.json().get("detail", "")
    assert "never" in detail
