"""YAML-based WorkflowDefinition loader (M8 T5/T6).

Scans `tasks/*.workflow.yaml` (note: the compound `.workflow.yaml` suffix
distinguishes M8 workflows from `.yaml` TaskDefs handled by
`csm.modules.automation.task_loader.TaskLoader`). Each file is parsed with
`csm.modules.workflow.schema.load_workflow_spec`, then upserted into the
`workflow_definition` table.

Idempotent: re-loading the same `file_path` updates the existing row.
If a different file declares an already-known `name`, raises
`WorkflowLoadError` — silent rebinding would let `file_path` churn between
sources on every reload (same policy as TaskLoader).

After upsert each row is fed through `review_workflow` (T6 R9-R12) and the
verdict is persisted to `review_status` / `review_report` / `reviewed_at`.
Reviewer never raises — its job is to record verdicts, not abort loading.
Only specs that pass review are compiled via `engine.compile_workflow` and
have `compiled_rules` populated; rejected specs leave that column NULL so
the orchestrator never launches a Mission against an unverified contract.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import WorkflowDefinition, WorkflowReviewStatus
from csm.modules.workflow.engine import compile_workflow
from csm.modules.workflow.reviewer import ReviewResult, review_workflow
from csm.modules.workflow.schema import WorkflowSchemaError, load_workflow_spec
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


WORKFLOW_GLOB = "*.workflow.yaml"


class WorkflowLoadError(Exception):
    """Raised on duplicate-name collision across different files.

    Schema-level errors propagate as `WorkflowSchemaError` from
    `load_workflow_spec`; per-file failures during `load_directory` are
    caught and logged at WARNING level so one bad YAML doesn't abort the
    whole scan.
    """


class WorkflowLoader:
    """Upsert WorkflowDefinitions from `*.workflow.yaml` files on disk."""

    def __init__(self, sessionmaker: async_sessionmaker):
        self._sm = sessionmaker

    async def load_file(self, path: Path) -> WorkflowDefinition:
        """Upsert a single workflow YAML file and run R9-R12 review.

        Raises `WorkflowSchemaError` on schema validation failure,
        `WorkflowLoadError` on duplicate-name collision with a different
        file_path. Review failures do NOT raise — they're recorded on the
        row so the UI can show `rejected` workflows for the user to fix.
        """
        p = Path(path)
        text = p.read_text(encoding="utf-8")
        spec = load_workflow_spec(text)
        review = review_workflow(spec)
        compiled = compile_workflow(spec) if review.status == "passed" else None
        return await self._upsert(
            spec,
            yaml_content=text,
            file_path=str(p.resolve()),
            review=review,
            compiled=compiled,
        )

    async def load_directory(self, directory: Path) -> list[WorkflowDefinition]:
        """Scan `directory` recursively for `*.workflow.yaml` and upsert each.

        Per-file failures (schema errors, IO errors) are logged at WARNING
        and skipped — the scan continues. Duplicate-name collisions still
        propagate as `WorkflowLoadError` because they signal author error
        the user must resolve (two files claiming the same workflow name).
        """
        d = Path(directory)
        if not d.is_dir():
            return []
        out: list[WorkflowDefinition] = []
        for p in sorted(d.rglob(WORKFLOW_GLOB)):
            try:
                row = await self.load_file(p)
                out.append(row)
            except WorkflowLoadError:
                raise
            except WorkflowSchemaError as e:
                log.warning("failed to load workflow %s: %s", p, e)
            except Exception as e:
                log.warning("failed to load workflow %s: %s", p, e)
        return out

    async def _upsert(
        self,
        spec,
        *,
        yaml_content: str,
        file_path: str,
        review: ReviewResult,
        compiled: dict | None,
    ) -> WorkflowDefinition:
        review_status = (
            WorkflowReviewStatus.PASSED
            if review.status == "passed"
            else WorkflowReviewStatus.REJECTED
        )
        review_report = review.to_dict()
        now = now_utc_naive()

        async with self._sm() as db:
            res = await db.execute(
                select(WorkflowDefinition).where(WorkflowDefinition.file_path == file_path)
            )
            row = res.scalar_one_or_none()

            if row is None:
                res = await db.execute(
                    select(WorkflowDefinition).where(WorkflowDefinition.name == spec.name)
                )
                row = res.scalar_one_or_none()
                if row is not None and row.file_path and row.file_path != file_path:
                    raise WorkflowLoadError(
                        f"duplicate workflow name {spec.name!r}: already bound to "
                        f"{row.file_path!r}; would now collide with {file_path!r}"
                    )

            if row is None:
                row = WorkflowDefinition(
                    name=spec.name,
                    description=spec.description,
                    file_path=file_path,
                    yaml_content=yaml_content,
                    compiled_rules=compiled,
                    review_status=review_status,
                    review_report=review_report,
                    reviewed_at=now,
                )
                db.add(row)
            else:
                row.name = spec.name
                row.description = spec.description
                row.file_path = file_path
                row.yaml_content = yaml_content
                row.compiled_rules = compiled
                row.review_status = review_status
                row.review_report = review_report
                row.reviewed_at = now
                row.updated_at = now
            await db.commit()
            await db.refresh(row)
            return row
