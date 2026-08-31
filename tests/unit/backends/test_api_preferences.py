"""API tests for `GET/PUT /api/preferences` + `POST /complete-first-run`."""
from __future__ import annotations

import pytest_asyncio
from csm.api.preferences import router as prefs_router
from csm.backends.registry import AdapterRegistry
from csm.models import Base
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tests.unit.backends._fake_adapter import FakeAdapter


@pytest_asyncio.fixture
async def app_and_client(tmp_path):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    app = FastAPI()
    app.state.sessionmaker = sm
    app.state.adapter_registry = AdapterRegistry([
        FakeAdapter("claude"),
        FakeAdapter("codex"),
    ])
    app.include_router(prefs_router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac
    await engine.dispose()


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


async def test_get_seeds_row_and_returns_defaults(app_and_client):
    resp = await app_and_client.get("/api/preferences")
    assert resp.status_code == 200
    body = resp.json()
    assert body["default_agent"] == "claude"
    assert body["supervisor_agent"] is None
    # Fresh in-memory DB has no seed migration; endpoint seeds with
    # has_completed_first_run=False (wizard trigger).
    assert body["has_completed_first_run"] is False
    assert body["is_first_run"] is True


# ---------------------------------------------------------------------------
# PUT — patch semantics
# ---------------------------------------------------------------------------


async def test_put_updates_default_agent(app_and_client):
    resp = await app_and_client.put(
        "/api/preferences", json={"default_agent": "codex"},
    )
    assert resp.status_code == 200
    assert resp.json()["default_agent"] == "codex"
    # Second GET reflects the write.
    get = await app_and_client.get("/api/preferences")
    assert get.json()["default_agent"] == "codex"


async def test_put_unknown_agent_400s(app_and_client):
    resp = await app_and_client.put(
        "/api/preferences", json={"default_agent": "gemini"},
    )
    assert resp.status_code == 400
    assert "gemini" in resp.json()["detail"]


async def test_put_sets_supervisor_agent(app_and_client):
    resp = await app_and_client.put(
        "/api/preferences", json={"supervisor_agent": "claude"},
    )
    assert resp.status_code == 200
    assert resp.json()["supervisor_agent"] == "claude"


async def test_put_clears_supervisor_with_null(app_and_client):
    # First set it
    await app_and_client.put(
        "/api/preferences", json={"supervisor_agent": "claude"},
    )
    # Then clear via explicit null
    resp = await app_and_client.put(
        "/api/preferences", json={"supervisor_agent": None},
    )
    assert resp.status_code == 200
    assert resp.json()["supervisor_agent"] is None


async def test_put_bad_supervisor_agent_400s(app_and_client):
    resp = await app_and_client.put(
        "/api/preferences", json={"supervisor_agent": "gemini"},
    )
    assert resp.status_code == 400


async def test_put_patch_only_touches_provided_fields(app_and_client):
    # Seed: set default_agent + supervisor_agent
    await app_and_client.put(
        "/api/preferences",
        json={"default_agent": "codex", "supervisor_agent": "claude"},
    )
    # PATCH only default_agent — supervisor must remain unchanged
    await app_and_client.put(
        "/api/preferences", json={"default_agent": "claude"},
    )
    body = (await app_and_client.get("/api/preferences")).json()
    assert body["default_agent"] == "claude"
    assert body["supervisor_agent"] == "claude"


# ---------------------------------------------------------------------------
# POST /complete-first-run
# ---------------------------------------------------------------------------


async def test_complete_first_run_flips_flag(app_and_client):
    resp = await app_and_client.post("/api/preferences/complete-first-run")
    assert resp.status_code == 200
    assert resp.json()["has_completed_first_run"] is True
    assert resp.json()["is_first_run"] is False


async def test_complete_first_run_is_idempotent(app_and_client):
    for _ in range(3):
        resp = await app_and_client.post("/api/preferences/complete-first-run")
        assert resp.status_code == 200
        assert resp.json()["has_completed_first_run"] is True


# ---------------------------------------------------------------------------
# default_session_prompt + enabled toggle
# ---------------------------------------------------------------------------


async def test_default_session_prompt_defaults_off_and_null(app_and_client):
    """Fresh install: prompt is null and toggle is off, so create_session
    never injects anything on spawn until the user opts in."""
    resp = await app_and_client.get("/api/preferences")
    body = resp.json()
    assert body["default_session_prompt"] is None
    assert body["default_session_prompt_enabled"] is False


async def test_put_default_session_prompt_persists(app_and_client):
    txt = "Prefer Unicode math (∑ ∫ α) over LaTeX."
    resp = await app_and_client.put("/api/preferences", json={
        "default_session_prompt": txt,
        "default_session_prompt_enabled": True,
    })
    assert resp.status_code == 200
    assert resp.json()["default_session_prompt"] == txt
    assert resp.json()["default_session_prompt_enabled"] is True

    # Idempotent GET returns same values
    resp2 = await app_and_client.get("/api/preferences")
    assert resp2.json()["default_session_prompt"] == txt
    assert resp2.json()["default_session_prompt_enabled"] is True


async def test_put_prompt_strips_whitespace_and_treats_empty_as_null(app_and_client):
    """`"   "` and `""` should both clear the prompt — otherwise the enabled
    toggle would send whitespace-only content on every session, which is
    both useless and confusing."""
    resp = await app_and_client.put("/api/preferences", json={
        "default_session_prompt": "   \n\t  ",
    })
    assert resp.status_code == 200
    assert resp.json()["default_session_prompt"] is None

    # Explicit null clears too
    await app_and_client.put("/api/preferences", json={
        "default_session_prompt": "real text",
    })
    resp = await app_and_client.put("/api/preferences", json={
        "default_session_prompt": None,
    })
    assert resp.json()["default_session_prompt"] is None


async def test_toggle_only_leaves_prompt_alone(app_and_client):
    """Present-fields-only patch semantics: flipping only the toggle must
    preserve the prompt text."""
    await app_and_client.put("/api/preferences", json={
        "default_session_prompt": "keep me",
        "default_session_prompt_enabled": False,
    })
    resp = await app_and_client.put("/api/preferences", json={
        "default_session_prompt_enabled": True,
    })
    assert resp.json()["default_session_prompt"] == "keep me"
    assert resp.json()["default_session_prompt_enabled"] is True


# ---------------------------------------------------------------------------
# default_session_prompt_note + enabled toggle
# ---------------------------------------------------------------------------


async def test_default_session_prompt_note_defaults_off_and_null(app_and_client):
    resp = await app_and_client.get("/api/preferences")
    body = resp.json()
    assert body["default_session_prompt_note"] is None
    assert body["default_session_prompt_note_enabled"] is False


async def test_put_default_session_prompt_note_persists(app_and_client):
    note = "以上为会话级默认提示，无需针对本条单独回复。"
    resp = await app_and_client.put("/api/preferences", json={
        "default_session_prompt_note": note,
        "default_session_prompt_note_enabled": True,
    })
    assert resp.status_code == 200
    assert resp.json()["default_session_prompt_note"] == note
    assert resp.json()["default_session_prompt_note_enabled"] is True

    resp2 = await app_and_client.get("/api/preferences")
    assert resp2.json()["default_session_prompt_note"] == note
    assert resp2.json()["default_session_prompt_note_enabled"] is True


async def test_put_note_strips_whitespace_and_treats_empty_as_null(app_and_client):
    resp = await app_and_client.put("/api/preferences", json={
        "default_session_prompt_note": "   \n\t  ",
    })
    assert resp.status_code == 200
    assert resp.json()["default_session_prompt_note"] is None


async def test_note_and_prompt_are_independent(app_and_client):
    """Patching the note must not disturb the prompt fields and vice versa —
    present-fields-only semantics across both pairs."""
    await app_and_client.put("/api/preferences", json={
        "default_session_prompt": "keep prompt",
        "default_session_prompt_enabled": True,
    })
    resp = await app_and_client.put("/api/preferences", json={
        "default_session_prompt_note": "keep note",
        "default_session_prompt_note_enabled": True,
    })
    body = resp.json()
    assert body["default_session_prompt"] == "keep prompt"
    assert body["default_session_prompt_enabled"] is True
    assert body["default_session_prompt_note"] == "keep note"
    assert body["default_session_prompt_note_enabled"] is True
