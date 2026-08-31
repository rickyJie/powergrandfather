"""Smoke CRUD tests for ORM models — verify they create / round-trip cleanly."""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime

import pytest
from csm.models import (
    AgentAlertRule,
    Base,
    FileState,
    HitObservation,
    Notification,
    Output,
    Session,
)
from csm.models.notification import NotificationType
from csm.models.output import OutputType
from csm.models.session import SessionStatus, SessionType
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


async def test_session_crud(db):
    async with db() as s:
        sess = Session(cwd="/tmp/p", type=SessionType.INTERACTIVE, status=SessionStatus.RUNNING, pid=1234)
        s.add(sess)
        await s.commit()
        got = await s.get(Session, sess.id)
        assert got is not None
        assert got.type == SessionType.INTERACTIVE
        assert got.status == SessionStatus.RUNNING


# NOTE: TaskDefinition + ScheduleEntry.task_def_id + Run.task_def_id
# were retired in commit bca23b8 (P2 refactor — workflow-only automation).
# The orphaned smoke tests here (test_task_def_crud / test_schedule_crud /
# test_run_crud) are deleted rather than fixed since the columns they
# exercised no longer exist. Schedule + Run are covered elsewhere via the
# workflow-oriented tests (test_scheduler_oneshot, test_runner_*).


async def test_output_crud(db):
    async with db() as s:
        o = Output(run_id=str(uuid.uuid4()), path="/tmp/f.md", type=OutputType.MARKDOWN)
        s.add(o)
        await s.commit()
        got = await s.get(Output, o.id)
        assert got is not None
        assert got.type == OutputType.MARKDOWN


async def test_notification_crud(db):
    async with db() as s:
        n = Notification(type=NotificationType.NEW_MESSAGE, title="hi")
        s.add(n)
        await s.commit()
        got = await s.get(Notification, n.id)
        assert got is not None
        assert got.type == NotificationType.NEW_MESSAGE


async def test_agent_alert_rule_crud(db):
    async with db() as s:
        a = AgentAlertRule(
            name="msgs>1000",
            nl_description="fire when 5h window msg count exceeds 1000",
            threshold_spec={"metric": "msg_count", "op": ">=", "value": 1000},
            check_script="def check(window):\n    return (window['msg_count'] >= 1000, {})\n",
            poll_interval_sec=60,
            channels=["inapp"],
        )
        s.add(a)
        await s.commit()
        got = await s.get(AgentAlertRule, a.id)
        assert got is not None
        assert got.threshold_spec["value"] == 1000
        assert got.channels == ["inapp"]
        assert got.escalate is False


async def test_hit_observation_crud(db):
    async with db() as s:
        h = HitObservation(ts=datetime.utcnow(), msg_count_5h=1397, cc_tokens_5h=13_300_000)
        s.add(h)
        await s.commit()
        got = await s.get(HitObservation, h.id)
        assert got is not None
        assert got.msg_count_5h == 1397


async def test_file_state_crud(db):
    async with db() as s:
        fs = FileState(artifact_path="/home/x/.claude/projects/p/abc.jsonl", last_offset=1024)
        s.add(fs)
        await s.commit()
        got = await s.get(FileState, fs.artifact_path)
        assert got is not None
        assert got.last_offset == 1024
