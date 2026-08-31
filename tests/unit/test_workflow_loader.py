"""Unit tests for csm.modules.workflow.loader (M8 / T5).

Coverage:
- load_directory scans `*.workflow.yaml`, persists valid files, logs+skips
  schema-invalid files (no exception bubbles).
- load_file persists a single YAML and the row carries name / file_path /
  yaml_content / review_status defaulting to PENDING.
- Re-loading the same file is idempotent (1 row stays 1 row).
- Two different files claiming the same workflow name → WorkflowLoadError.
- `*.yaml` files without the `.workflow.yaml` suffix are ignored (so we
  don't trample TaskDefinition YAMLs in the same directory).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
from csm.models import Base, WorkflowDefinition, WorkflowReviewStatus
from csm.modules.workflow.loader import WorkflowLoader, WorkflowLoadError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Minimal valid workflow YAML (per PRD §3, trimmed to the bare minimum)
# ---------------------------------------------------------------------------

VALID_YAML_TEMPLATE = """
name: {name}
description: minimal workflow
stages:
  - name: design
    kind: claude
    prompt: hello world
    outputs:
      - "{{ws}}/design.md"
"""

INVALID_YAML = """
name: bogus
stages:
  - name: design
    kind: not_a_real_kind
    prompt: x
    outputs:
      - foo
"""

TASKDEF_YAML = """
name: not_a_workflow
cwd: /tmp
prompt: hi
"""


@pytest.fixture
async def sm():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    yield sessionmaker
    await engine.dispose()
    os.unlink(db_path)


async def test_load_file_persists_workflow(sm, tmp_path: Path):
    p = tmp_path / "good.workflow.yaml"
    p.write_text(VALID_YAML_TEMPLATE.format(name="alpha"))
    loader = WorkflowLoader(sm)
    row = await loader.load_file(p)
    assert row.name == "alpha"
    assert row.file_path == str(p.resolve())
    assert "stages:" in row.yaml_content
    # T6: a minimal-valid workflow now passes R9-R12 review on load.
    assert row.review_status == WorkflowReviewStatus.PASSED
    assert row.review_report is not None
    assert row.review_report["status"] == "passed"
    assert {r["rule_id"] for r in row.review_report["rules"]} == {
        "R9", "R10", "R11", "R12", "R13",
        "R14", "R15", "R16", "R17", "R18", "R19",
    }
    assert row.reviewed_at is not None
    # T8: loader now compiles workflows that pass review and stores the JSON blob.
    assert row.compiled_rules is not None
    assert row.compiled_rules["workflow_name"] == "alpha"
    assert "design" in row.compiled_rules["stages"]


async def test_load_directory_skips_invalid_and_persists_valid(sm, tmp_path: Path):
    (tmp_path / "good.workflow.yaml").write_text(VALID_YAML_TEMPLATE.format(name="alpha"))
    (tmp_path / "bad.workflow.yaml").write_text(INVALID_YAML)
    loader = WorkflowLoader(sm)
    rows = await loader.load_directory(tmp_path)
    assert len(rows) == 1
    assert rows[0].name == "alpha"
    async with sm() as db:
        persisted = (await db.execute(select(WorkflowDefinition))).scalars().all()
    assert len(persisted) == 1
    assert persisted[0].name == "alpha"


async def test_load_directory_idempotent_upsert(sm, tmp_path: Path):
    p = tmp_path / "iter.workflow.yaml"
    p.write_text(VALID_YAML_TEMPLATE.format(name="iter_wf"))
    loader = WorkflowLoader(sm)
    rows1 = await loader.load_directory(tmp_path)
    rows2 = await loader.load_directory(tmp_path)
    assert len(rows1) == 1
    assert len(rows2) == 1
    assert rows1[0].id == rows2[0].id
    async with sm() as db:
        persisted = (await db.execute(select(WorkflowDefinition))).scalars().all()
    assert len(persisted) == 1


async def test_duplicate_name_in_two_files_raises(sm, tmp_path: Path):
    (tmp_path / "a.workflow.yaml").write_text(VALID_YAML_TEMPLATE.format(name="same_name"))
    (tmp_path / "b.workflow.yaml").write_text(VALID_YAML_TEMPLATE.format(name="same_name"))
    loader = WorkflowLoader(sm)
    with pytest.raises(WorkflowLoadError):
        await loader.load_directory(tmp_path)


async def test_plain_yaml_files_are_ignored(sm, tmp_path: Path):
    """A `*.yaml` file that is NOT `*.workflow.yaml` (e.g. a TaskDef) must be
    skipped — otherwise the workflow loader would mis-parse TaskDef YAMLs
    that share the directory."""
    (tmp_path / "taskdef.yaml").write_text(TASKDEF_YAML)
    (tmp_path / "real.workflow.yaml").write_text(VALID_YAML_TEMPLATE.format(name="real_one"))
    loader = WorkflowLoader(sm)
    rows = await loader.load_directory(tmp_path)
    assert len(rows) == 1
    assert rows[0].name == "real_one"


async def test_load_directory_returns_empty_for_missing_dir(sm, tmp_path: Path):
    loader = WorkflowLoader(sm)
    rows = await loader.load_directory(tmp_path / "does_not_exist")
    assert rows == []
