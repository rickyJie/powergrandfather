"""APScheduler integration: register/unregister cron- and date-triggered
workflow launches.

Post-P2 (workflow-only): every ScheduleEntry targets a WorkflowDefinition
via `workflow_def_id`. Cron ticks dispatch to
`orchestrator.launch_mission(workflow_name, params)`.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import ScheduleEntry, WorkflowDefinition
from csm.utils.time import now_utc_naive

log = logging.getLogger(__name__)


def parse_cron(expr: str) -> CronTrigger:
    """Accept 5-field cron strings ('M H DoM Mon DoW')."""
    parts = expr.strip().split()
    if len(parts) != 5:
        raise ValueError(f"cron expression must have 5 fields, got {len(parts)}: {expr!r}")
    minute, hour, dom, mon, dow = parts
    return CronTrigger(minute=minute, hour=hour, day=dom, month=mon, day_of_week=dow)


class AutomationScheduler:
    """Thin wrapper over APScheduler keyed by ScheduleEntry.id.

    All schedule rows now bind to a WorkflowDefinition. The `runner`
    argument is kept for backwards-compat with lifespan wiring but is
    unused after M4 retirement — only the orchestrator is invoked at
    fire time.
    """

    def __init__(self, sessionmaker: async_sessionmaker, runner: Any = None, orchestrator: Any = None):
        self._sm = sessionmaker
        self._runner = runner
        self._orch = orchestrator
        self._sched = AsyncIOScheduler()

    async def start(self) -> None:
        # Idempotent start (FastAPI auto-reload or test reuse can call twice).
        if self._sched.running:
            return
        # Hydrate from DB — reap orphan schedules whose workflow is gone
        # (otherwise APScheduler would tick forever for a dead workflow_def_id).
        from sqlalchemy import delete as sa_delete
        async with self._sm() as db:
            res = await db.execute(select(ScheduleEntry).where(ScheduleEntry.enabled.is_(True)))
            rows = list(res.scalars().all())
            live_wf_ids = set(
                (await db.execute(select(WorkflowDefinition.id))).scalars().all()
            )
            orphan_ids: list[str] = []
            for r in rows:
                if r.workflow_def_id is None or r.workflow_def_id not in live_wf_ids:
                    orphan_ids.append(r.id)
            if orphan_ids:
                await db.execute(sa_delete(ScheduleEntry).where(ScheduleEntry.id.in_(orphan_ids)))
                await db.commit()
                log.warning("reaped %d orphan schedule(s): %s", len(orphan_ids), orphan_ids)
        for r in rows:
            if r.id in set(orphan_ids):
                continue
            try:
                self._register_job(r)
            except Exception as e:
                log.warning("skipping bad schedule %s: %s", r.id, e)
        self._sched.start()

    async def stop(self) -> None:
        if self._sched.running:
            self._sched.shutdown(wait=False)

    def _register_job(self, row: ScheduleEntry) -> None:
        if row.run_at is not None:
            # one-shot: skip registration if already in the past
            if row.run_at <= now_utc_naive():
                log.info("schedule %s run_at is in the past, skipping", row.id)
                return
            # DB stores naive UTC; APScheduler interprets naive datetimes as
            # **system local**, not UTC. Without an explicit tz-aware value
            # a 12:05 UTC run_at fires at 12:05 CST = 8h late. Make it UTC.
            run_at_utc = row.run_at.replace(tzinfo=UTC)
            trigger = DateTrigger(run_date=run_at_utc)
        elif row.cron:
            trigger = parse_cron(row.cron)
        else:
            log.warning("schedule %s has neither cron nor run_at, skipping", row.id)
            return
        self._sched.add_job(
            self._fire_run,
            trigger=trigger,
            args=[row.id],
            id=row.id,
            replace_existing=True,
            misfire_grace_time=120,
            coalesce=True,
            max_instances=1,
        )

    async def _fire_run(self, schedule_id: str) -> None:
        async with self._sm() as db:
            row = await db.get(ScheduleEntry, schedule_id)
            if row is None or not row.enabled:
                return
            workflow_def_id = row.workflow_def_id
            params = row.parameters or {}
            is_one_shot = row.run_at is not None
        try:
            if workflow_def_id is None:
                log.warning("schedule %s has no workflow_def_id — orphan",
                            schedule_id)
                return
            if self._orch is None:
                log.error("schedule %s cannot fire: no orchestrator wired",
                          schedule_id)
                return
            async with self._sm() as db:
                wf = await db.get(WorkflowDefinition, workflow_def_id)
                if wf is None:
                    log.warning("schedule %s: workflow %s gone", schedule_id, workflow_def_id)
                    return
                wf_name = wf.name
            await self._orch.launch_mission(wf_name, dict(params))
        except Exception as e:
            log.exception("scheduled mission launch failed for %s: %s", schedule_id, e)
        # One-shot schedules auto-disable after firing so they don't clutter
        # the calendar — the resulting Mission row keeps the history.
        if is_one_shot:
            async with self._sm() as db:
                row = await db.get(ScheduleEntry, schedule_id)
                if row is not None:
                    row.enabled = False
                    row.last_run_at = now_utc_naive()
                    await db.commit()

    # ---- CRUD ----
    async def add_schedule(
        self,
        workflow_def_id: str,
        cron_expr: str | None = None,
        parameters: dict | None = None,
        run_at: datetime | None = None,
    ) -> ScheduleEntry:
        if (cron_expr is None) == (run_at is None):
            raise ValueError("exactly one of cron_expr / run_at must be provided")
        if not workflow_def_id:
            raise ValueError("workflow_def_id is required")
        if cron_expr is not None:
            parse_cron(cron_expr)  # validate before save
        async with self._sm() as db:
            row = ScheduleEntry(
                workflow_def_id=workflow_def_id,
                cron=cron_expr,
                run_at=run_at,
                enabled=True,
                parameters=parameters or {},
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
        self._register_job(row)
        return row

    async def set_enabled(self, schedule_id: str, enabled: bool) -> ScheduleEntry | None:
        async with self._sm() as db:
            row = await db.get(ScheduleEntry, schedule_id)
            if row is None:
                return None
            row.enabled = enabled
            await db.commit()
            await db.refresh(row)
        if enabled:
            self._register_job(row)
        else:
            try:
                self._sched.remove_job(schedule_id)
            except Exception:
                pass
        return row

    async def delete_schedule(self, schedule_id: str) -> bool:
        try:
            self._sched.remove_job(schedule_id)
        except Exception:
            pass
        async with self._sm() as db:
            row = await db.get(ScheduleEntry, schedule_id)
            if row is None:
                return False
            await db.delete(row)
            await db.commit()
        return True
