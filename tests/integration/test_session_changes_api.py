"""Integration tests for GET /api/sessions/{sid}/changes[/diff].

Verifies end-to-end wiring: DB row → JSONL path resolution →
parse_edits_from_jsonl → API response shape. The parser itself is
already unit-tested in tests/unit/test_session_changes.py; here we
focus on the plumbing (404s, empty responses, path resolution).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from csm.api.sessions import router as sessions_router
from csm.backends import build_default_registry
from csm.core.event_stream import EventStream
from csm.models import Base
from csm.models import Session as SessRow
from csm.models.session import SessionStatus, SessionType
from csm.modules.session_manager.manager import SessionManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def api_client(tmp_path, monkeypatch):
    # Point settings.claude_projects_dir at a fresh temp so we can plant
    # transcripts under the exact encoded-cwd folder claude would use.
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()
    codex_sessions_dir = tmp_path / "codex-sessions"
    codex_sessions_dir.mkdir()
    from csm.config import settings as _settings
    monkeypatch.setattr(_settings, "claude_projects_dir", projects_dir)
    monkeypatch.setattr(_settings, "codex_sessions_dir", codex_sessions_dir)
    monkeypatch.setattr(_settings, "session_output_dir", tmp_path / "session-output")

    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    es = EventStream(projects_root=projects_dir, poll_interval_sec=10.0, watchdog_interval_sec=10.0)
    mgr = SessionManager(
        sessionmaker=sessionmaker, event_stream=es,
        adapter_registry=build_default_registry(),
        ring_buffer_bytes=4096, stop_grace_sec=1,
        claude_argv=["bash", "-i"],
    )
    app = FastAPI()
    app.state.sessionmaker = sessionmaker
    app.state.session_manager = mgr
    app.include_router(sessions_router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, sessionmaker, projects_dir

    await mgr.shutdown()
    await es.stop()
    await engine.dispose()
    os.unlink(db_path)


async def _plant_session(sm, projects_dir: Path, cwd: str, claude_sid: str, edits: list[dict]) -> str:
    """Insert a Session row + write its JSONL transcript with the given tool_use blocks."""
    from csm.modules.agent.jsonl_fast_tail import conversation_jsonl_path

    stamps = [
        datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(UTC).replace(tzinfo=None)
        for ts, _name, _inp in edits
    ]
    async with sm() as db:
        row = SessRow(
            cwd=cwd,
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,  # doesn't matter, just needs a row
            pid=None,
            external_session_id=claude_sid,
            started_at=min(stamps) - timedelta(seconds=1) if stamps else None,
            ended_at=max(stamps) + timedelta(seconds=1) if stamps else None,
        )
        db.add(row)
        await db.commit()
        sid = row.id

    # Plant the JSONL where conversation_jsonl_path would look for it.
    jp = conversation_jsonl_path(projects_dir, cwd, claude_sid)
    jp.parent.mkdir(parents=True, exist_ok=True)
    with jp.open("w", encoding="utf-8") as fh:
        for ts, name, inp in edits:
            fh.write(json.dumps({
                "type": "assistant",
                "timestamp": ts,
                "message": {"content": [{"type": "tool_use", "id": f"tuid_{ts}", "name": name, "input": inp}]},
            }) + "\n")
    return sid


async def _plant_codex_session(
    sm,
    codex_sessions_dir: Path,
    *,
    cwd: str,
    external_session_id: str | None,
    calls: list[tuple[str, str, str, int]],
    bind_rollout: bool = True,
    started_at: datetime | None = None,
) -> str:
    """Insert a Codex Session row and a rollout containing apply_patch calls."""
    rollout_id = external_session_id or "legacy-rollout-id"
    rollout = (
        codex_sessions_dir
        / "2026/07/30"
        / f"rollout-2026-07-30T10-00-00-{rollout_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    meta_ts = "2026-07-30T10:00:01Z"
    with rollout.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "type": "session_meta",
            "timestamp": meta_ts,
            "payload": {
                "session_id": rollout_id,
                "cwd": cwd,
                "timestamp": meta_ts,
            },
        }) + "\n")
        if started_at is not None and external_session_id is None:
            fh.write(json.dumps({
                "type": "event_msg",
                "timestamp": "2026-07-30T10:00:20Z",
                "payload": {"type": "user_message", "message": "redacted"},
            }) + "\n")
        for ts, call_id, patch, exit_code in calls:
            fh.write(json.dumps({
                "type": "response_item",
                "timestamp": ts,
                "payload": {
                    "type": "custom_tool_call",
                    "name": "apply_patch",
                    "call_id": call_id,
                    "input": patch,
                },
            }) + "\n")
            fh.write(json.dumps({
                "type": "response_item",
                "timestamp": ts,
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": call_id,
                    "output": (
                        f"Exit code: {exit_code}\n"
                        + (
                            "Success. Updated the following files:\nM file\n"
                            if exit_code == 0
                            else "Output:\nFailed to find expected lines\n"
                        )
                    ),
                },
            }) + "\n")

    async with sm() as db:
        row = SessRow(
            cwd=cwd,
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            pid=None,
            agent="codex",
            external_session_id=external_session_id,
            rollout_path=str(rollout) if bind_rollout else None,
            started_at=started_at or datetime.fromisoformat("2026-07-30T10:00:00"),
        )
        db.add(row)
        await db.commit()
        return row.id


async def test_list_changes_returns_files_sorted_by_recency(api_client):
    """A session with edits to two files should list them last-touched-first."""
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-abc", [
        ("2026-07-25T09:00:00Z", "Edit", {"file_path": "/a.py", "old_string": "x", "new_string": "y"}),
        ("2026-07-25T10:00:00Z", "Write", {"file_path": "/b.md", "content": "hi"}),
        ("2026-07-25T11:00:00Z", "Edit", {"file_path": "/a.py", "old_string": "y", "new_string": "z"}),
    ])

    r = await client.get(f"/api/sessions/{sid}/changes")
    assert r.status_code == 200
    body = r.json()
    assert body["sid"] == sid
    assert body["total_edits"] == 3
    paths = [f["path"] for f in body["files"]]
    assert paths == ["/a.py", "/b.md"]  # /a.py wins on last_ts
    a = body["files"][0]
    assert a["edit_count"] == 2
    assert a["tools"] == ["Edit"]
    assert a["additions"] == 2
    assert a["deletions"] == 2
    assert a["change_kind"] == "modified"


async def test_list_endpoint_reports_pagination_metadata(api_client):
    client, sm, projects_dir = api_client
    for index in range(3):
        await _plant_session(
            sm, projects_dir, f"/tmp/proj-{index}", f"sid-page-{index}", [],
        )
    first = await client.get(
        "/api/sessions",
        params={"status": "exited", "type": "interactive", "limit": 2},
    )
    assert first.status_code == 200
    body = first.json()
    assert body["count"] == 3
    assert body["page_count"] == 2
    assert body["offset"] == 0
    assert body["has_more"] is True

    second = await client.get(
        "/api/sessions",
        params={"status": "exited", "type": "interactive", "limit": 2, "offset": 2},
    )
    assert second.status_code == 200
    assert second.json()["page_count"] == 1
    assert second.json()["has_more"] is False


async def test_output_endpoint_returns_persisted_binary_snapshot(api_client):
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/output", "sid-output", [])
    manager = client._transport.app.state.session_manager
    manager._persist_output_snapshot(sid, b"\x1b[32mfinal output\x1b[0m\r\n")
    response = await client.get(f"/api/sessions/{sid}/output")
    assert response.status_code == 200
    assert response.headers["x-csm-output-source"] == "persisted"
    assert b"final output" in response.content


async def test_archive_is_reversible_and_purge_refuses_live_rows(api_client):
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/archive", "sid-archive", [])
    archived = await client.patch(f"/api/sessions/{sid}", json={"archived": True})
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None
    restored = await client.patch(f"/api/sessions/{sid}", json={"archived": False})
    assert restored.status_code == 200
    assert restored.json()["archived_at"] is None

    bulk = await client.post("/api/sessions/archive-ended")
    assert bulk.status_code == 200
    assert bulk.json()["archived"] == 1
    async with sm() as db:
        assert (await db.get(SessRow, sid)).archived_at is not None

    async with sm() as db:
        live = SessRow(
            cwd="/tmp",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            pid=os.getpid(),
        )
        db.add(live)
        await db.commit()
        live_id = live.id
    refused = await client.post(f"/api/sessions/{live_id}/purge")
    assert refused.status_code == 409
    async with sm() as db:
        assert await db.get(SessRow, live_id) is not None


async def test_purge_history_bulk_wipes_ended_leaves_live_alone(api_client):
    """/api/sessions/purge-history clears every interactive+ended row in one
    shot, leaves live rows untouched, and doesn't touch AUTO rows."""
    client, sm, projects_dir = api_client
    # Plant three ended interactive sessions (mix of exited/crashed).
    ended_ids: list[str] = []
    for i in range(3):
        sid = await _plant_session(
            sm, projects_dir, f"/tmp/ended-{i}", f"sid-ended-{i}", []
        )
        async with sm() as db:
            row = await db.get(SessRow, sid)
            row.status = SessionStatus.EXITED if i % 2 == 0 else SessionStatus.CRASHED
            await db.commit()
        ended_ids.append(sid)

    # Plant a live interactive session — must survive.
    async with sm() as db:
        live = SessRow(
            cwd="/tmp/live",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.RUNNING,
            pid=os.getpid(),
        )
        db.add(live)
        await db.commit()
        live_id = live.id

    # Plant an AUTO ended row — must also survive (AUTO isn't "history").
    async with sm() as db:
        auto = SessRow(
            cwd="/tmp/auto",
            type=SessionType.AUTO,
            status=SessionStatus.EXITED,
        )
        db.add(auto)
        await db.commit()
        auto_id = auto.id

    r = await client.post("/api/sessions/purge-history")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["purged"] == 3
    assert sorted(body["ids"]) == sorted(ended_ids)

    async with sm() as db:
        for sid in ended_ids:
            assert await db.get(SessRow, sid) is None
        assert await db.get(SessRow, live_id) is not None
        assert await db.get(SessRow, auto_id) is not None

    # Second call is idempotent — 0 purged, no error.
    r2 = await client.post("/api/sessions/purge-history")
    assert r2.status_code == 200
    assert r2.json() == {"purged": 0, "ids": []}


async def test_legacy_codex_resume_recovers_id_from_rollout(
    api_client, monkeypatch,
):
    client, sm, _projects_dir = api_client
    from csm.config import settings

    old_id = await _plant_codex_session(
        sm,
        settings.codex_sessions_dir,
        cwd="/tmp",
        external_session_id=None,
        calls=[],
        bind_rollout=False,
        started_at=datetime.fromisoformat("2026-07-30T10:00:15"),
    )
    manager = client._transport.app.state.session_manager
    captured: dict[str, str] = {}

    async def _fake_create_session(**kwargs):
        captured["resume_from"] = kwargs["resume_from"]
        captured["agent"] = kwargs["agent"]
        async with sm() as db:
            fresh = SessRow(
                cwd=kwargs["cwd"],
                type=SessionType.INTERACTIVE,
                status=SessionStatus.RUNNING,
                agent=kwargs["agent"],
                external_session_id=kwargs["resume_from"],
            )
            db.add(fresh)
            await db.commit()
            await db.refresh(fresh)
            return fresh

    monkeypatch.setattr(manager, "create_session", _fake_create_session)
    response = await client.post(f"/api/sessions/{old_id}/resume")
    assert response.status_code == 200, response.text
    assert captured == {
        "resume_from": "legacy-rollout-id",
        "agent": "codex",
    }
    async with sm() as db:
        old = await db.get(SessRow, old_id)
        assert old.external_session_id == "legacy-rollout-id"
        assert old.rollout_path
        assert old.superseded_by == response.json()["id"]


async def test_diff_returns_ordered_edits_for_one_file(api_client):
    """GET .../changes/diff?path=X returns only that file's edits, in time order."""
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-diff", [
        ("2026-07-25T09:00:00Z", "Edit", {"file_path": "/a.py", "old_string": "1", "new_string": "2"}),
        ("2026-07-25T10:00:00Z", "Edit", {"file_path": "/b.py", "old_string": "x", "new_string": "y"}),
        ("2026-07-25T11:00:00Z", "Edit", {"file_path": "/a.py", "old_string": "2", "new_string": "3"}),
    ])

    r = await client.get(f"/api/sessions/{sid}/changes/diff", params={"path": "/a.py"})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/a.py"
    assert len(body["edits"]) == 2
    assert body["edits"][0]["old"] == "1"
    assert body["edits"][0]["new"] == "2"
    assert body["edits"][1]["old"] == "2"
    assert body["edits"][1]["new"] == "3"
    assert [e["index"] for e in body["edits"]] == [0, 1]


async def test_resumed_rows_only_report_edits_from_their_own_time_window(api_client):
    client, sm, projects_dir = api_client
    cwd = "/tmp/resume-chain"
    external_id = "shared-conversation"
    first_id = await _plant_session(sm, projects_dir, cwd, external_id, [
        ("2026-07-25T09:00:00Z", "Edit", {
            "file_path": "/first.py", "old_string": "a", "new_string": "b",
        }),
        ("2026-07-25T11:00:00Z", "Edit", {
            "file_path": "/second.py", "old_string": "x", "new_string": "y",
        }),
    ])
    async with sm() as db:
        first = await db.get(SessRow, first_id)
        first.started_at = datetime.fromisoformat("2026-07-25T08:59:50")
        first.ended_at = datetime.fromisoformat("2026-07-25T09:00:10")
        second = SessRow(
            cwd=cwd,
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            external_session_id=external_id,
            started_at=datetime.fromisoformat("2026-07-25T10:59:50"),
            ended_at=datetime.fromisoformat("2026-07-25T11:00:10"),
        )
        db.add(second)
        await db.commit()
        second_id = second.id

    first_body = (await client.get(f"/api/sessions/{first_id}/changes")).json()
    second_body = (await client.get(f"/api/sessions/{second_id}/changes")).json()
    assert [item["path"] for item in first_body["files"]] == ["/first.py"]
    assert [item["path"] for item in second_body["files"]] == ["/second.py"]


async def test_write_edit_reports_null_old_string(api_client):
    """Write tool → old=None so the frontend can label as 'New file'."""
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-w", [
        ("2026-07-25T12:00:00Z", "Write", {"file_path": "/new.md", "content": "# hi"}),
    ])
    r = await client.get(f"/api/sessions/{sid}/changes/diff", params={"path": "/new.md"})
    body = r.json()
    assert body["edits"][0]["old"] is None
    assert body["edits"][0]["new"] == "# hi"
    assert body["edits"][0]["tool"] == "Write"


async def test_session_without_jsonl_returns_empty_not_404(api_client):
    """Row exists but no transcript on disk (claude pruned it or the sid
    was never real) → 200 with empty files list. 404 would break the
    frontend panel for a legitimate row."""
    client, sm, _projects_dir = api_client
    async with sm() as db:
        row = SessRow(
            cwd="/tmp/nowhere",
            type=SessionType.INTERACTIVE,
            status=SessionStatus.EXITED,
            external_session_id="sid-with-no-transcript",
        )
        db.add(row)
        await db.commit()
        sid = row.id

    r = await client.get(f"/api/sessions/{sid}/changes")
    assert r.status_code == 200
    assert r.json()["files"] == []
    assert r.json()["total_edits"] == 0


async def test_unknown_session_returns_404(api_client):
    """Row genuinely doesn't exist → 404 (distinct from "row exists but empty")."""
    client, _sm, _projects_dir = api_client
    r = await client.get("/api/sessions/does-not-exist/changes")
    assert r.status_code == 404


async def test_diff_for_unknown_path_returns_empty(api_client):
    """Asking for a path claude never touched in this session → empty edits.
    Not 404 — polling races (asked before the turn landed) shouldn't fail."""
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-nomatch", [
        ("2026-07-25T09:00:00Z", "Edit", {"file_path": "/a.py", "old_string": "x", "new_string": "y"}),
    ])
    r = await client.get(f"/api/sessions/{sid}/changes/diff", params={"path": "/never-touched.py"})
    assert r.status_code == 200
    assert r.json()["edits"] == []


async def test_codex_changes_parse_only_successful_apply_patches(api_client):
    client, sm, projects_dir = api_client
    codex_sessions_dir = projects_dir.parent / "codex-sessions"
    successful = """*** Begin Patch
*** Update File: src/app.py
@@
-old
+new
*** Add File: notes.md
+hello
*** End Patch"""
    failed = """*** Begin Patch
*** Add File: should-not-appear.txt
+nope
*** End Patch"""
    sid = await _plant_codex_session(
        sm,
        codex_sessions_dir,
        cwd="/workspace",
        external_session_id="codex-session-1",
        calls=[
            ("2026-07-30T10:01:00Z", "call-ok", successful, 0),
            ("2026-07-30T10:02:00Z", "call-failed", failed, 1),
        ],
    )

    response = await client.get(f"/api/sessions/{sid}/changes")
    assert response.status_code == 200
    body = response.json()
    assert body["total_edits"] == 2
    assert {item["path"] for item in body["files"]} == {
        "/workspace/src/app.py",
        "/workspace/notes.md",
    }
    assert all("should-not-appear" not in item["path"] for item in body["files"])

    diff = await client.get(
        f"/api/sessions/{sid}/changes/diff",
        params={"path": "/workspace/src/app.py"},
    )
    assert diff.status_code == 200
    assert diff.json()["edits"] == [{
        "index": 0,
        "ts": "2026-07-30T10:01:00Z",
        "tool": "ApplyPatch",
        "old": "old",
        "new": "new",
        "tool_use_id": "call-ok",
        "sub_index": 0,
        "source_path": None,
    }]


async def test_codex_changes_recovers_stale_path_by_external_id(api_client):
    client, sm, projects_dir = api_client
    codex_sessions_dir = projects_dir.parent / "codex-sessions"
    sid = await _plant_codex_session(
        sm,
        codex_sessions_dir,
        cwd="/workspace",
        external_session_id="codex-session-fallback",
        calls=[(
            "2026-07-30T10:01:00Z",
            "call-ok",
            """*** Begin Patch
*** Add File: recovered.txt
+found
*** End Patch""",
            0,
        )],
        bind_rollout=False,
    )

    response = await client.get(f"/api/sessions/{sid}/changes")
    assert response.status_code == 200
    assert response.json()["files"][0]["path"] == "/workspace/recovered.txt"


async def test_codex_changes_recovers_legacy_unbound_session_by_time(api_client):
    client, sm, projects_dir = api_client
    codex_sessions_dir = projects_dir.parent / "codex-sessions"
    sid = await _plant_codex_session(
        sm,
        codex_sessions_dir,
        cwd="/workspace",
        external_session_id=None,
        calls=[(
            "2026-07-30T10:00:21Z",
            "call-ok",
            """*** Begin Patch
*** Add File: legacy.txt
+legacy
*** End Patch""",
            0,
        )],
        bind_rollout=False,
        started_at=datetime.fromisoformat("2026-07-30T10:00:00"),
    )

    response = await client.get(f"/api/sessions/{sid}/changes")
    assert response.status_code == 200
    assert response.json()["files"][0]["path"] == "/workspace/legacy.txt"


async def test_diff_view_falls_back_to_per_edit_when_disk_file_missing(api_client):
    """Cumulative reconstruction needs the disk file. When it's missing
    the renderer must fall back to the per-edit-block layout inside the
    file section — not crash and not silently emit an empty page."""
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-view", [
        ("2026-07-25T09:00:00Z", "Edit", {
            "file_path": "/proj/main-diff-test.py",
            "old_string": "def hello():\n    print('hi')\n",
            "new_string": "def hello():\n    print('hello')\n",
        }),
        ("2026-07-25T10:00:00Z", "Edit", {
            "file_path": "/proj/main-diff-test.py",
            "old_string": "print('hello')",
            "new_string": "print('HELLO!')",
        }),
    ])

    r = await client.get(
        f"/api/sessions/{sid}/changes/diff-view",
        params={"path": "/proj/main-diff-test.py"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    body = r.text
    assert "<!DOCTYPE html>" in body
    # New multi-file layout: file section + per-edit fallback blocks inside
    assert 'id="file-1"' in body
    assert 'file-section' in body
    assert 'edit-block' in body  # per-edit fallback blocks inside the section
    # Mode badge signals per-edit path chosen
    assert 'per-edit' in body
    # Table renderer emitted at least one diff row class
    assert 'class="diff-table"' in body


async def test_diff_view_whole_file_when_disk_file_reconstructable(api_client, tmp_path):
    """When disk file exists and reverse-apply succeeds, the renderer
    diffs the reconstructed session-start snapshot against current file
    with `n` set to file length — every unchanged line renders as a
    context row between the changed rows (VS Code inline diff feel).
    Mode badge reads `whole file`."""
    client, sm, projects_dir = api_client
    real = tmp_path / "cumul.py"
    real.write_text("def hello():\n    print('HELLO!')\n")
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-cumul", [
        ("2026-07-25T09:00:00Z", "Edit", {
            "file_path": str(real),
            "old_string": "def hello():\n    print('hi')\n",
            "new_string": "def hello():\n    print('hello')\n",
        }),
        ("2026-07-25T10:00:00Z", "Edit", {
            "file_path": str(real),
            "old_string": "print('hello')",
            "new_string": "print('HELLO!')",
        }),
    ])
    # `full=1` opts into whole-file context (default is compact ±6 hunks).
    # This test predates that default, and its purpose is to prove that
    # reconstruction can still deliver whole-file context when the disk
    # file is intact — so we ask for it explicitly.
    r = await client.get(
        f"/api/sessions/{sid}/changes/diff-view",
        params={"path": str(real), "full": "1"},
    )
    assert r.status_code == 200
    body = r.text
    # Section id + mode badge
    assert 'id="file-1"' in body
    assert 'whole file' in body
    # Table markup + at least one changed row
    assert 'class="diff-table"' in body
    assert 'diff-add' in body or 'diff-del' in body
    # Both the removed `'hi'` and the final `HELLO` should appear.
    # Pygments wraps string literals in spans, so we check for the
    # unadorned identifier text rather than the raw `'hi'` substring.
    assert ">hi<" in body or "hi&#" in body or "'hi'" in body
    assert "HELLO" in body


async def test_diff_view_default_is_compact_hunks(api_client, tmp_path):
    """Default diff mode is `±6 context` (compact GitHub-style hunks) so
    large files don't force reviewers to scroll through unchanged lines.
    Adding `?full=1` restores the whole-file behavior tested elsewhere."""
    client, sm, projects_dir = api_client
    real = tmp_path / "compact.py"
    real.write_text("a\nb\nc\nd\nE\nf\ng\nh\ni\nj\nk\n")
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-compact", [
        ("2026-07-25T09:00:00Z", "Edit", {
            "file_path": str(real),
            "old_string": "e\n", "new_string": "E\n",
        }),
    ])
    r = await client.get(
        f"/api/sessions/{sid}/changes/diff-view",
        params={"path": str(real)},
    )
    assert r.status_code == 200
    body = r.text
    # Default mode badge shows the context radius, not "whole file".
    assert "\u00b16 context" in body
    assert "whole file" not in body
    # Toggle link to opt into full context must be present.
    assert "Show full context" in body
    # Keyboard hint strip.
    assert "<kbd>j</kbd>" in body and "<kbd>[</kbd>" in body
    # New JS surfaces: collapse toggle button + scroll-spy hook.
    assert "file-collapse-btn" in body
    assert "data-file-anchor" in body


async def test_diff_view_context_bar_has_back_to_session(api_client, tmp_path):
    """diff-view header injects the session context bar so reviewers can
    return to the SPA session view without hunting through browser tabs."""
    client, sm, projects_dir = api_client
    real = tmp_path / "ctx.py"
    real.write_text("x = 1\n")
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-ctx", [
        ("2026-07-25T09:00:00Z", "Edit", {
            "file_path": str(real),
            "old_string": "x = 0\n", "new_string": "x = 1\n",
        }),
    ])
    r = await client.get(f"/api/sessions/{sid}/changes/diff-view")
    assert r.status_code == 200
    body = r.text
    assert "csm-ctx-bar" in body
    assert "Back to session" in body
    assert f"/sessions/{sid}" in body


async def test_diff_view_multi_file_lists_all_touched_files(api_client, tmp_path):
    """No `?path=` → all files the session touched appear as separate
    file sections in one page, plus a sidebar with anchors + counts."""
    client, sm, projects_dir = api_client
    real1 = tmp_path / "a.py"
    real1.write_text("x = 1\n")
    real2 = tmp_path / "b.py"
    real2.write_text("y = 2\n")
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-multi", [
        ("2026-07-25T09:00:00Z", "Edit", {
            "file_path": str(real1), "old_string": "x = 0\n", "new_string": "x = 1\n",
        }),
        ("2026-07-25T10:00:00Z", "Edit", {
            "file_path": str(real2), "old_string": "y = 0\n", "new_string": "y = 2\n",
        }),
    ])
    r = await client.get(f"/api/sessions/{sid}/changes/diff-view")
    assert r.status_code == 200
    body = r.text
    # Both files present as anchor sections
    assert 'id="file-1"' in body
    assert 'id="file-2"' in body
    # Sidebar rendered with links to each anchor
    assert 'diff-sidebar' in body
    assert 'href="#file-1"' in body
    assert 'href="#file-2"' in body
    # Both filenames appear in sidebar
    assert 'a.py' in body
    assert 'b.py' in body


async def test_diff_view_for_write_shows_full_new_content_as_additions(api_client):
    """Write records have no baseline; reverse-apply refuses and falls
    back to per-edit view. The per-edit renderer synthesises a
    `@@ -0,0 +1,N @@` header so the whole content renders as additions."""
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-wview", [
        ("2026-07-25T11:00:00Z", "Write", {
            "file_path": "/proj/new.md",
            "content": "# Title\n\nBody line.\n",
        }),
    ])
    r = await client.get(f"/api/sessions/{sid}/changes/diff-view", params={"path": "/proj/new.md"})
    assert r.status_code == 200
    body = r.text
    assert "#1 of 1" in body
    assert "Write" in body
    assert 'tool-write' in body
    # Table renderer emitted at least one add row for the Write content
    # (the synthesised `@@ -0,0 +1,N @@` header + `+`-prefixed body lines).
    assert 'class="diff-add"' in body
    assert 'class="diff-hunk"' in body
    # The actual Write content shows up in the diff body (pygments may
    # wrap `Title` in a span, so search for the unadorned identifier).
    assert "Title" in body
    assert "Body line" in body


async def test_diff_view_empty_for_path_renders_state_card_not_404(api_client):
    """Path with no edits → HTML page with 'No edits recorded' state card.
    404 would look like an infrastructure error to the user."""
    client, sm, projects_dir = api_client
    sid = await _plant_session(sm, projects_dir, "/tmp/proj", "sid-empty-view", [])
    r = await client.get(f"/api/sessions/{sid}/changes/diff-view", params={"path": "/nowhere"})
    assert r.status_code == 200
    body = r.text
    assert "No edits recorded" in body
