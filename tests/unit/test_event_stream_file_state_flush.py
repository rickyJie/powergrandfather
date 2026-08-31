"""EventStream's tail-offset persistence must not monopolise the SQLite writer.

`_flush_file_state` used to upsert EVERY tracked artifact on every flush, one
statement at a time, inside one transaction. SQLite takes the write lock on the
first statement of a transaction and holds it to the commit, so the pass was an
exclusive hold whose length grew with the size of the transcript corpus.

Measured on the live box 2026-08-27 with 15,581 tracked files: the write lock
was unavailable 24% of the time, in ~9-second blocks every 30s, while inserting
zero rows — every one was an unchanged UPDATE. The old code stamped
`updated_at` per row, so the table recorded the hold directly: 15,581 DISTINCT
stamps climbing from 04:59:14.607282 to 04:59:23.538832, one pass lasting 8.93
seconds. Every other writer queued behind it: the notification INSERT for a
finished turn, claude's hooks, worktime heartbeats, mark-read. Reads were
untouched (WAL readers don't block) — 1 slow read in 21,864 against 24.3% of
writes over a second.

12,838 of those rows (82.4%) pointed at transcripts that had been deleted, so
volume is the other half of the story — hence the prune tests here too.

So the invariants under test are about WRITE VOLUME, not just final state:
an unchanged file must produce no statement at all, a batch must go out as one
executemany rather than one round-trip per file, and a row must never be
recorded as durable before its commit.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from csm.core.event_stream import EventStream
from csm.models import Base, FileState
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


class _Tailer:
    """Stands in for JsonlTailer — only `snapshot()` matters to the flush."""

    def __init__(self) -> None:
        self.snap: dict[str, dict[str, Any]] = {}

    def snapshot(self) -> dict[str, Any]:
        return self.snap

    def restore(self, snap: dict[str, Any]) -> None:
        self.snap = dict(snap)


class _Writes:
    """Counts the INSERT statements a flush actually issues."""

    def __init__(self, engine) -> None:
        self.stmts: list[tuple[bool, int]] = []  # (executemany, row_count)
        event.listen(engine.sync_engine, "before_cursor_execute", self._on)

    def _on(self, conn, cursor, statement, parameters, context, executemany):
        if "INSERT INTO file_state" not in statement:
            return
        n = len(parameters) if executemany else 1
        self.stmts.append((executemany, n))

    @property
    def count(self) -> int:
        return len(self.stmts)

    @property
    def rows(self) -> int:
        return sum(n for _, n in self.stmts)

    def reset(self) -> None:
        self.stmts.clear()


@pytest_asyncio.fixture
async def es(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'state.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    stream = EventStream(
        projects_root=tmp_path,
        sessionmaker=sm,
        flush_interval_sec=0,
        seed_stale_history_as_idle=False,
    )
    stream._tailer = _Tailer()
    yield stream, sm, _Writes(engine)
    await engine.dispose()


def _files(n: int, offset: int = 10) -> dict[str, dict[str, Any]]:
    return {
        f"/tmp/p/{i}.jsonl": {"offset": offset, "mtime": 1.0 + i, "line_no": 0}
        for i in range(n)
    }


async def _rows(sm) -> dict[str, tuple[int, Any]]:
    async with sm() as db:
        rows = (await db.execute(select(FileState))).scalars().all()
        return {r.artifact_path: (r.last_offset, r.updated_at) for r in rows}


@pytest.mark.asyncio
async def test_unchanged_offsets_produce_no_write_at_all(es):
    """The regression itself: a quiet console must not touch the writer.

    Not "must write the same values cheaply" — must not open a write
    transaction. Every statement issued here is time the notification INSERT
    for a finished turn spends queued.
    """
    stream, sm, writes = es
    stream._tailer.snap = _files(5)

    await stream._flush_file_state()
    assert writes.rows == 5, "first flush has to persist them"

    writes.reset()
    await stream._flush_file_state()
    await stream._flush_file_state()

    assert writes.count == 0


@pytest.mark.asyncio
async def test_only_the_file_that_moved_is_rewritten(es):
    """One appended transcript must not drag the other 15k rows with it."""
    stream, sm, writes = es
    stream._tailer.snap = _files(4)
    await stream._flush_file_state()
    before = await _rows(sm)

    writes.reset()
    stream._tailer.snap["/tmp/p/2.jsonl"] = {"offset": 999, "mtime": 42.0, "line_no": 0}
    await stream._flush_file_state()

    assert writes.rows == 1
    after = await _rows(sm)
    assert after["/tmp/p/2.jsonl"][0] == 999
    for path in ("/tmp/p/0.jsonl", "/tmp/p/1.jsonl", "/tmp/p/3.jsonl"):
        assert after[path] == before[path], f"{path} was rewritten for no reason"


@pytest.mark.asyncio
async def test_a_late_session_id_binding_is_persisted_even_though_the_offset_held(es):
    """"Changed" can't mean "the offset moved".

    A codex rollout's session id is discovered post-hoc, and that can land on a
    tick where the file did not grow. Skipping the write there would leave the
    binding unpersisted until the file next happened to be appended to.
    """
    stream, sm, writes = es
    stream._tailer.snap = {
        "/tmp/p/r.jsonl": {"offset": 10, "mtime": 1.0, "line_no": 0, "session_id": None},
    }
    await stream._flush_file_state()

    writes.reset()
    stream._tailer.snap["/tmp/p/r.jsonl"]["session_id"] = "codex-abc"
    await stream._flush_file_state()

    assert writes.rows == 1
    async with sm() as db:
        row = (await db.execute(select(FileState))).scalars().one()
    assert row.session_id == "codex-abc"
    assert row.last_offset == 10


@pytest.mark.asyncio
async def test_a_batch_goes_out_as_one_executemany(es):
    """Volume, not just correctness: 50 files must not be 50 round-trips.

    Each statement is its own aiosqlite thread hop, and they all sit inside one
    write transaction — which is how a per-file loop turned into a nine-second
    exclusive lock hold.
    """
    stream, sm, writes = es
    stream._tailer.snap = _files(50)

    await stream._flush_file_state()

    assert writes.count == 1
    assert writes.stmts[0] == (True, 50)


@pytest.mark.asyncio
async def test_large_first_flush_is_chunked(es, monkeypatch):
    """The one genuinely big pass (fresh DB) still can't hold the lock forever."""
    monkeypatch.setattr("csm.core.event_stream._FILE_STATE_FLUSH_CHUNK", 10)
    stream, sm, writes = es
    stream._tailer.snap = _files(25)

    await stream._flush_file_state()

    assert [n for _, n in writes.stmts] == [10, 10, 5]
    assert len(await _rows(sm)) == 25


class _FailNthWrite:
    """Wraps the sessionmaker so the Nth session's `execute` raises.

    Failing the statement (not the statement *builder*) is the whole point: an
    earlier version of the test below monkeypatched `_file_state_upsert`, which
    the flush calls ONCE before the chunk loop — so it blew up the entire pass
    and never exercised a chunk boundary at all.
    """

    def __init__(self, sm, fail_on: int) -> None:
        self._sm, self.fail_on, self.calls = sm, fail_on, 0

    def __call__(self):
        self.calls += 1
        return _Ctx(self._sm(), self.calls == self.fail_on)


class _Ctx:
    def __init__(self, session, boom: bool) -> None:
        self._session, self._boom = session, boom

    async def __aenter__(self):
        db = await self._session.__aenter__()
        return _Boom(db) if self._boom else db

    async def __aexit__(self, *exc):
        return await self._session.__aexit__(*exc)


class _Boom:
    def __init__(self, db) -> None:
        self._db = db

    async def execute(self, *a, **k):
        raise RuntimeError("writer unavailable")

    def __getattr__(self, name):
        return getattr(self._db, name)


@pytest.mark.asyncio
async def test_a_chunk_that_failed_leaves_the_mirror_honest_and_is_retried(
    es, monkeypatch
):
    """The mirror must never claim a row is durable that never committed.

    This is the highest-risk path in the change: `_durable_file_state` is
    trusted without re-reading the table, so a row wrongly marked durable is
    not a lost write that retries — it is a row that stops being written
    FOREVER, and whose transcript therefore re-tails from byte 0 after the
    next restart.
    """
    monkeypatch.setattr("csm.core.event_stream._FILE_STATE_FLUSH_CHUNK", 10)
    stream, sm, writes = es
    stream._tailer.snap = _files(25)
    stream._durable_file_state = {}  # as if restore had seeded an empty table

    failing = _FailNthWrite(sm, fail_on=2)  # second chunk
    stream._sm = failing

    await stream._flush_file_state()

    first_ten = {f"/tmp/p/{i}.jsonl" for i in range(10)}
    assert set(await _rows(sm)) == first_ten, "chunk 1 must have committed"
    assert set(stream._durable_file_state) == first_ten, (
        "the mirror recorded rows that never reached the DB"
    )
    assert failing.calls == 2, "the pass must stop at the failed chunk"

    failing.fail_on = -1
    await stream._flush_file_state()

    assert len(await _rows(sm)) == 25
    assert len(stream._durable_file_state) == 25


@pytest.mark.asyncio
async def test_restore_seeds_the_diff_so_a_restart_does_not_rewrite_everything(es):
    """After a restart the DB already holds these offsets — re-persisting all of
    them is the exact 9-second pass this change exists to remove."""
    stream, sm, writes = es
    stream._tailer.snap = _files(6)
    await stream._flush_file_state()

    reborn = EventStream(
        projects_root=Path("/tmp"),
        sessionmaker=sm,
        flush_interval_sec=0,
        seed_stale_history_as_idle=False,
    )
    reborn._tailer = _Tailer()
    await reborn._restore_file_state()

    writes.reset()
    await reborn._flush_file_state()
    assert writes.count == 0


async def _seed(sm, rows: list[tuple[str, str]]) -> None:
    """rows: (artifact_path, agent)."""
    async with sm() as db:
        for path, agent in rows:
            db.add(FileState(
                artifact_path=path, agent=agent,
                last_offset=10, last_mtime=1.0, session_id=None,
            ))
        await db.commit()


@pytest.mark.asyncio
async def test_restore_forgets_rows_whose_transcript_is_gone(es, tmp_path):
    """The table only ever grew — 82.4% of it was rows for deleted files.

    Making the write cheap does not shrink the set: the boot read, the
    tailer's map and the mirror all still carry it. Prune has to happen where
    all three are derived, or they disagree.
    """
    stream, sm, _writes = es
    alive = tmp_path / "alive.jsonl"
    alive.write_text("{}\n")
    await _seed(sm, [
        (str(alive), "claude"),
        (str(tmp_path / "deleted-a.jsonl"), "claude"),
        (str(tmp_path / "deleted-b.jsonl"), "claude"),
    ])

    await stream._restore_file_state()

    assert set(await _rows(sm)) == {str(alive)}
    assert set(stream._durable_file_state) == {str(alive)}
    assert set(stream._tailer.snapshot()) == {str(alive)}


@pytest.mark.asyncio
async def test_an_absent_tree_is_kept_not_pruned(es, tmp_path):
    """Every artifact missing means an unmounted filesystem, not 15k deletions.

    Forgetting those offsets would re-tail every transcript from byte 0 on the
    next tick and replay every event in it — far worse than a stale row.
    """
    stream, sm, _writes = es
    gone = [(str(tmp_path / f"gone-{i}.jsonl"), "claude") for i in range(4)]
    await _seed(sm, gone)

    await stream._restore_file_state()

    assert len(await _rows(sm)) == 4, "an away mount must not be treated as deletion"
    assert len(stream._durable_file_state) == 4


@pytest.mark.asyncio
async def test_one_agents_absent_tree_does_not_block_anothers_prune(es, tmp_path):
    """The guard is per-agent: codex being away can't pin claude's dead rows."""
    stream, sm, _writes = es
    alive = tmp_path / "claude-alive.jsonl"
    alive.write_text("{}\n")
    await _seed(sm, [
        (str(alive), "claude"),
        (str(tmp_path / "claude-dead.jsonl"), "claude"),
        (str(tmp_path / "codex-away-1.jsonl"), "codex"),
        (str(tmp_path / "codex-away-2.jsonl"), "codex"),
    ])

    await stream._restore_file_state()

    remaining = set(await _rows(sm))
    assert str(tmp_path / "claude-dead.jsonl") not in remaining
    assert str(tmp_path / "codex-away-1.jsonl") in remaining
    assert str(tmp_path / "codex-away-2.jsonl") in remaining


@pytest.mark.asyncio
async def test_tail_loop_holds_a_fixed_period_when_the_tick_is_slow(es, monkeypatch):
    """The sleep must be the remainder of the period, not sit on top of it.

    Detection latency for a turn no Stop hook reported (every codex session,
    plus claude sessions whose hook didn't land) is exactly one period, so
    drift here lands on the user as a late notification.
    """
    stream, _sm, _writes = es
    stream._poll_interval = 0.30
    stream._sm = None  # take the flush out of the measurement
    starts: list[float] = []

    async def slow_tick():
        starts.append(time.monotonic())
        await asyncio.sleep(0.18)

    monkeypatch.setattr(stream, "_tick_once", slow_tick)

    task = asyncio.create_task(stream._tail_loop())
    await asyncio.sleep(1.05)
    stream._stopping.set()
    await asyncio.wait_for(task, timeout=2)

    assert len(starts) >= 3, f"only {len(starts)} ticks — cadence is too slow"
    periods = [b - a for a, b in zip(starts, starts[1:], strict=False)]
    # 0.30 total, not 0.30 + 0.18. Generous upper bound so a loaded CI box
    # doesn't flake, but still well under the old 0.48.
    for p in periods:
        assert 0.25 <= p <= 0.40, f"period {p:.3f}s is not the configured 0.30s"
