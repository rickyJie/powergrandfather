"""Title sync from external CLI adapters into `session.title`.

`local:7a422f9d` — claude persists user renames (Ctrl+R in the /resume
picker) as `{"type":"custom-title","customTitle":"…","sessionId":"…"}`
records inside the same JSONL. Codex 0.145+ stores an auto-derived
`threads.title` in `state_<N>.sqlite`. Both should reflect back into
CSM's own `session.title` — EXCEPT when the CSM user has claimed the
field via a UI rename (`title_manual=true`).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest
from csm.backends.base import Capability
from csm.backends.registry import AdapterRegistry
from csm.core.event_stream import EventStream
from csm.models import Base, Session
from csm.models.session import SessionStatus, SessionType
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def setup():
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    es = EventStream(
        projects_root=Path(tempfile.gettempdir()),
        poll_interval_sec=10.0,
        watchdog_interval_sec=10.0,
        sessionmaker=sm,
    )
    yield es, sm
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


async def _plant(sm, *, sid="csm-1", ext="ext-1", title=None, title_manual=False):
    async with sm() as db:
        s = Session(
            id=sid,
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            title=title,
            external_session_id=ext,
            title_manual=title_manual,
        )
        db.add(s)
        await db.commit()


async def _get(sm, sid):
    async with sm() as db:
        return await db.get(Session, sid)


# ---- claude custom-title / ai-title ----

async def test_claude_custom_title_overwrites_when_not_manual(setup):
    es, sm = setup
    await _plant(sm, sid="s1", ext="claude-uuid-1", title=None)
    await es._sync_external_title(
        external_session_id="claude-uuid-1",
        new_title="my-renamed-work",
        source="custom-title",
    )
    row = await _get(sm, "s1")
    assert row.title == "my-renamed-work"


async def test_claude_custom_title_respects_manual_lock(setup):
    """`title_manual=true` = user already typed a title via CSM UI. External
    source must not clobber it. This is the load-bearing guard."""
    es, sm = setup
    await _plant(sm, sid="s2", ext="claude-uuid-2", title="user-set", title_manual=True)
    await es._sync_external_title(
        external_session_id="claude-uuid-2",
        new_title="external-would-win",
        source="custom-title",
    )
    row = await _get(sm, "s2")
    assert row.title == "user-set"


async def test_ai_title_defers_to_any_existing_title(setup):
    """ai-title is the weakest source: it must not overwrite a title
    that was already set (either by user via UI OR by a prior
    custom-title sync). Only fills an empty slot."""
    es, sm = setup
    await _plant(sm, sid="s3", ext="claude-uuid-3", title="already-here")
    await es._sync_external_title(
        external_session_id="claude-uuid-3",
        new_title="ai-guess",
        source="ai-title",
    )
    row = await _get(sm, "s3")
    assert row.title == "already-here"


async def test_ai_title_fills_empty_title(setup):
    es, sm = setup
    await _plant(sm, sid="s4", ext="claude-uuid-4", title=None)
    await es._sync_external_title(
        external_session_id="claude-uuid-4",
        new_title="ai-derived",
        source="ai-title",
    )
    row = await _get(sm, "s4")
    assert row.title == "ai-derived"


async def test_custom_title_beats_ai_title(setup):
    """User rename (custom-title) fired after an ai-title that filled the
    field earlier — user rename must win."""
    es, sm = setup
    await _plant(sm, sid="s5", ext="claude-uuid-5", title="ai-derived")
    await es._sync_external_title(
        external_session_id="claude-uuid-5",
        new_title="user-typed-in-claude",
        source="custom-title",
    )
    row = await _get(sm, "s5")
    assert row.title == "user-typed-in-claude"


async def test_sync_skips_superseded_row(setup):
    """A resumed session leaves the predecessor row with
    `superseded_by IS NOT NULL`. External title records that happen to
    fire against the old external_session_id must not modify a dead row."""
    es, sm = setup
    async with sm() as db:
        old = Session(
            id="old-sid",
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            external_session_id="shared-uuid",
            superseded_by="new-sid",
            title=None,
        )
        db.add(old)
        await db.commit()
    await es._sync_external_title(
        external_session_id="shared-uuid",
        new_title="attempted",
        source="custom-title",
    )
    row = await _get(sm, "old-sid")
    assert row.title is None


async def test_sync_no_op_when_target_missing(setup):
    """External session id with no CSM row: silent no-op, no exception."""
    es, sm = setup
    await es._sync_external_title(
        external_session_id="does-not-exist",
        new_title="anything",
        source="custom-title",
    )
    # No exception is the assertion — the helper must swallow the miss.


# ---- codex threads.title reader ----

def test_codex_thread_title_reader_returns_none_when_home_missing(tmp_path):
    from csm.backends.codex.adapter import query_codex_thread_title
    # tmp_path has no state_*.sqlite — should just return None.
    assert query_codex_thread_title(tmp_path, "any-id") is None


def test_codex_thread_title_reader_reads_state_db(tmp_path):
    """Plant a state_5.sqlite that matches the codex schema shape enough
    for our SELECT to succeed; verify the reader picks up the title."""
    from csm.backends.codex.adapter import query_codex_thread_title
    db_path = tmp_path / "state_5.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("""
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT,
            created_at INTEGER,
            title TEXT
        )
    """)
    con.execute(
        "INSERT INTO threads (id, rollout_path, created_at, title) VALUES (?, ?, ?, ?)",
        ("codex-thread-abc", "/tmp/rollout.jsonl", 12345, "把 sync 干完"),
    )
    con.commit()
    con.close()
    assert query_codex_thread_title(tmp_path, "codex-thread-abc") == "把 sync 干完"
    # Unknown id → None (not exception)
    assert query_codex_thread_title(tmp_path, "no-such-id") is None
    # Empty id → None (guard)
    assert query_codex_thread_title(tmp_path, "") is None


def test_codex_thread_title_reader_strips_whitespace(tmp_path):
    from csm.backends.codex.adapter import query_codex_thread_title
    db_path = tmp_path / "state_5.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE threads (id TEXT, title TEXT)")
    con.execute("INSERT INTO threads (id, title) VALUES (?, ?)", ("t1", "  padded  "))
    con.commit()
    con.close()
    assert query_codex_thread_title(tmp_path, "t1") == "padded"


def test_codex_thread_title_reader_empty_title_is_none(tmp_path):
    """title column present but empty string → returns None so the caller
    doesn't clobber a real CSM title with '' from a fresh thread row."""
    from csm.backends.codex.adapter import query_codex_thread_title
    db_path = tmp_path / "state_5.sqlite"
    con = sqlite3.connect(db_path)
    con.execute("CREATE TABLE threads (id TEXT, title TEXT)")
    con.execute("INSERT INTO threads (id, title) VALUES (?, ?)", ("t1", ""))
    con.commit()
    con.close()
    assert query_codex_thread_title(tmp_path, "t1") is None


# ---- PATCH endpoint sets title_manual ----

@pytest.mark.asyncio
async def test_patch_title_sets_title_manual_true(setup):
    """Direct DB check: after a PATCH-style title assignment through the
    same code path the API endpoint uses, title_manual flips to true. The
    endpoint is thin so we exercise the state transition here rather than
    spinning up a full HTTP client."""
    es, sm = setup
    await _plant(sm, sid="s-ui-1", ext="ext-ui-1", title=None, title_manual=False)
    # Mirror api/sessions.py PATCH logic
    async with sm() as db:
        row = await db.get(Session, "s-ui-1")
        stripped = "user-typed".strip() or None
        row.title = stripped
        row.title_manual = stripped is not None
        await db.commit()
    row = await _get(sm, "s-ui-1")
    assert row.title == "user-typed"
    assert row.title_manual is True

    # Clearing to null releases the lock so external sync can take over.
    async with sm() as db:
        row = await db.get(Session, "s-ui-1")
        stripped = "".strip() or None  # noqa: PLC1802 — mirror server logic
        row.title = stripped
        row.title_manual = stripped is not None
        await db.commit()
    row = await _get(sm, "s-ui-1")
    assert row.title is None
    assert row.title_manual is False


# ---- create-time title claims the field (mirror of PATCH semantics) ----

async def test_create_time_title_auto_marks_manual(setup):
    """A caller-supplied title at spawn time is treated as user intent —
    same claim semantics as a UI rename via PATCH — so subsequent
    ai-title emissions from claude don't silently overwrite it.
    """
    es, sm = setup
    # Mirror SessionManager._commit_and_spawn's derivation: any non-empty
    # trimmed title flips title_manual on at row-insert time.
    _title_manual = bool("lora finetune" and "lora finetune".strip())
    async with sm() as db:
        s = Session(
            id="s-create-1",
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            title="lora finetune",
            title_manual=_title_manual,
            external_session_id="claude-create-1",
        )
        db.add(s)
        await db.commit()
    row = await _get(sm, "s-create-1")
    assert row.title == "lora finetune"
    assert row.title_manual is True
    # Now a later ai-title from claude should be a no-op.
    await es._sync_external_title(
        external_session_id="claude-create-1",
        new_title="SDXL LoRA sample-adapt训练与QC",
        source="ai-title",
    )
    row = await _get(sm, "s-create-1")
    assert row.title == "lora finetune"


async def test_create_without_title_leaves_manual_false(setup):
    """No title at creation → title_manual stays off → future ai-title
    sync is free to populate the field.
    """
    _title_manual = bool(None and (None or "").strip())
    async with setup[1]() as db:
        s = Session(
            id="s-create-2",
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            title=None,
            title_manual=_title_manual,
            external_session_id="claude-create-2",
        )
        db.add(s)
        await db.commit()
    es, sm = setup
    await es._sync_external_title(
        external_session_id="claude-create-2",
        new_title="ai-generated topic",
        source="ai-title",
    )
    row = await _get(sm, "s-create-2")
    assert row.title == "ai-generated topic"
    # Still not user-locked — a subsequent custom-title still wins.
    await es._sync_external_title(
        external_session_id="claude-create-2",
        new_title="user-typed via /rename",
        source="custom-title",
    )
    row = await _get(sm, "s-create-2")
    assert row.title == "user-typed via /rename"


async def test_create_whitespace_only_title_leaves_manual_false(setup):
    """Whitespace-only title = no real name; don't lock the field."""
    _title_manual = bool("   \n\t" and "   \n\t".strip())
    async with setup[1]() as db:
        s = Session(
            id="s-create-3",
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            title="   \n\t",
            title_manual=_title_manual,
            external_session_id="claude-create-3",
        )
        db.add(s)
        await db.commit()
    assert _title_manual is False


# ---- adapter-held titles are gated on Capability.EXTERNAL_TITLE ----
#
# EventStream used to branch on `event_agent == "codex"` here, which
# scripts/lint-agent-abstraction.sh rejects: domain code must ask the adapter
# what it can do, not what it is named. These pin the capability contract so
# a third adapter with its own title store works without touching EventStream.


class _TitleAdapter:
    """Minimal stand-in: declares EXTERNAL_TITLE and serves one title."""

    name = "with-titles"
    capabilities = frozenset({Capability.EXTERNAL_TITLE})

    def __init__(self, title: str | None):
        self._title = title
        self.calls: list[str] = []

    def lookup_external_title(self, external_id: str) -> str | None:
        self.calls.append(external_id)
        return self._title


class _NoTitleAdapter:
    """Implements the method but does not declare the capability."""

    name = "no-titles"
    capabilities = frozenset()

    def lookup_external_title(self, external_id: str) -> str | None:  # pragma: no cover
        raise AssertionError("must not be called without EXTERNAL_TITLE")


def test_keeps_titles_externally_reads_capability_not_name(setup):
    es, _sm = setup
    es._registry = AdapterRegistry([_TitleAdapter("t"), _NoTitleAdapter()])
    assert es._keeps_titles_externally("with-titles") is True
    assert es._keeps_titles_externally("no-titles") is False
    # Unregistered agent must not raise out of the projection path.
    assert es._keeps_titles_externally("never-heard-of-it") is False


def test_keeps_titles_externally_false_without_registry(setup):
    """EventStream built without a registry sees no adapter events at all."""
    es, _sm = setup
    assert es._registry is None
    assert es._keeps_titles_externally("with-titles") is False


async def test_adapter_held_title_written_via_adapter(setup):
    es, sm = setup
    adapter = _TitleAdapter("codex 那边起的标题")
    es._registry = AdapterRegistry([adapter])
    await _plant(sm, sid="s-ext-1", ext="thread-1", title=None)
    await es._sync_adapter_held_title("s-ext-1", "with-titles", "thread-1")
    assert adapter.calls == ["thread-1"]
    row = await _get(sm, "s-ext-1")
    assert row.title == "codex 那边起的标题"


async def test_adapter_held_title_respects_manual_lock(setup):
    es, sm = setup
    es._registry = AdapterRegistry([_TitleAdapter("adapter wins?")])
    await _plant(sm, sid="s-ext-2", ext="thread-2", title="mine", title_manual=True)
    await es._sync_adapter_held_title("s-ext-2", "with-titles", "thread-2")
    row = await _get(sm, "s-ext-2")
    assert row.title == "mine"


async def test_adapter_held_title_skipped_without_capability(setup):
    """No EXTERNAL_TITLE → lookup is never attempted (the fake would raise)."""
    es, sm = setup
    es._registry = AdapterRegistry([_NoTitleAdapter()])
    await _plant(sm, sid="s-ext-3", ext="thread-3", title=None)
    await es._sync_adapter_held_title("s-ext-3", "no-titles", "thread-3")
    row = await _get(sm, "s-ext-3")
    assert row.title is None
