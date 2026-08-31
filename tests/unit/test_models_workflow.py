"""Smoke CRUD tests for the M8 workflow ORM models."""
from __future__ import annotations

import os
import tempfile

import pytest
from csm.models import Base, Mission, MissionStatus, WorkflowDefinition, WorkflowReviewStatus
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()
    os.unlink(path)


def test_imports():
    # Importable from the package surface, including enums.
    assert WorkflowDefinition is not None
    assert Mission is not None
    assert MissionStatus.RUNNING == "running"
    assert MissionStatus.PAUSED == "paused"
    assert set(MissionStatus) == {
        MissionStatus.PENDING,
        MissionStatus.RUNNING,
        MissionStatus.PAUSED,
        MissionStatus.CANCELLED,
        MissionStatus.SUCCEEDED,
        MissionStatus.FAILED,
    }


async def test_workflow_definition_crud(db):
    async with db() as s:
        wf = WorkflowDefinition(
            name="sample_experiment",
            description="end-to-end sample pipeline experiment",
            file_path="/tmp/sample_experiment.workflow.yaml",
            yaml_content="name: sample_experiment\n",
            compiled_rules={"stages": {"design": {"validation": []}}},
        )
        s.add(wf)
        await s.commit()
        got = await s.get(WorkflowDefinition, wf.id)
        assert got is not None
        assert got.name == "sample_experiment"
        assert got.review_status == WorkflowReviewStatus.PENDING
        assert got.compiled_rules == {"stages": {"design": {"validation": []}}}
        assert got.created_at is not None
        assert got.updated_at is not None


async def test_mission_crud_with_fk(db):
    async with db() as s:
        wf = WorkflowDefinition(
            name="sample_experiment",
            file_path="/tmp/sample_experiment.workflow.yaml",
            yaml_content="name: sample_experiment\n",
        )
        s.add(wf)
        await s.commit()

        mission = Mission(
            workflow_def_id=wf.id,
            parameters={"topic": "DropPath p=0.15", "baseline": "current_sota"},
            workspace_path="/data/projects/sample_pipeline/.claude/missions/m1",
            status=MissionStatus.RUNNING,
            current_stage="design",
            audit_log=[{"ts": "2026-06-30T00:00:00Z", "event": "launched"}],
        )
        s.add(mission)
        await s.commit()

        got = await s.get(Mission, mission.id)
        assert got is not None
        assert got.workflow_def_id == wf.id
        assert got.status == MissionStatus.RUNNING
        assert got.current_stage == "design"
        assert got.parameters["topic"] == "DropPath p=0.15"
        assert got.audit_log[0]["event"] == "launched"
        assert got.created_at is not None
        # nullable terminal fields default to None
        assert got.started_at is None
        assert got.ended_at is None
        assert got.failure_reason is None
