"""API tests for /api/settings/lark — GET / PUT / POST test.

Uses an in-memory aiosqlite + `LarkSettings.__table__.create` to
isolate from the full migration chain. Fake LarkSink stubs `send_test`
and `flush_dedup_cache` so we can assert wiring without shelling out.
"""
from __future__ import annotations

import shutil

import pytest
import pytest_asyncio
from csm.api.lark_settings import router as lark_router
from csm.models.lark_settings import LarkSettings
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool


class _FakeSink:
    """Test double: records send_test calls + flush counts."""

    def __init__(self):
        self.send_test_calls: list[str] = []
        self.flush_count: int = 0
        # Default: report a successful shell send. Overridable per test.
        self._send_test_result = (True, None, 0.05)

    async def send_test(self, note: str = "") -> tuple[bool, str | None, float]:
        self.send_test_calls.append(note)
        return self._send_test_result

    def flush_dedup_cache(self, **_) -> int:
        self.flush_count += 1
        return 0


@pytest_asyncio.fixture
async def client_and_sink(monkeypatch):
    """Build an app with just the lark_settings router + in-mem DB."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(LarkSettings.__table__.create)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    sink = _FakeSink()

    app = FastAPI()
    app.state.sessionmaker = sm
    app.state.lark_sink = sink
    app.include_router(lark_router)

    # Force cli_installed=True for the general case; individual tests
    # can re-monkeypatch shutil.which as needed.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/lark-cli")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, sink, sm

    await engine.dispose()


async def _seed(sm, **overrides):
    defaults = dict(
        id=1, enabled=False, chat_id=None, user_id=None,
        dedup_window_sec=60, dnd_hours=[], tz=None,
        enabled_types={"session_crashed": True, "auto_run_failed": True,
                       "token_warning": True, "port_conflict": True},
    )
    defaults.update(overrides)
    async with sm() as db:
        row = await db.get(LarkSettings, 1)
        if row is None:
            db.add(LarkSettings(**defaults))
        else:
            for k, v in defaults.items():
                if k != "id":
                    setattr(row, k, v)
        await db.commit()


# ---------- GET ----------
@pytest.mark.asyncio
async def test_get_returns_synthetic_default_when_row_missing(client_and_sink):
    client, _, _ = client_and_sink
    r = await client.get("/api/settings/lark")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["chat_id"] is None
    assert body["user_id"] is None
    assert body["cli_installed"] is True
    # All known types present, all False
    for t in ("new_message", "session_crashed", "mission_done"):
        assert t in body["enabled_types"]
        assert body["enabled_types"][t] is False


@pytest.mark.asyncio
async def test_get_returns_seeded_row(client_and_sink):
    client, _, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_abc", dedup_window_sec=120)
    r = await client.get("/api/settings/lark")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is True
    assert body["chat_id"] == "oc_abc"
    assert body["dedup_window_sec"] == 120
    assert body["enabled_types"]["session_crashed"] is True
    # Backfill: unknown-to-seed types are shown as False
    assert body["enabled_types"]["new_message"] is False


@pytest.mark.asyncio
async def test_get_reflects_cli_not_installed(client_and_sink, monkeypatch):
    client, _, _ = client_and_sink
    monkeypatch.setattr(shutil, "which", lambda _: None)
    r = await client.get("/api/settings/lark")
    assert r.json()["cli_installed"] is False


# ---------- PUT: validation ----------
@pytest.mark.asyncio
async def test_put_enable_without_target_returns_400(client_and_sink):
    """v1 P0: enabled=True + no chat_id + no user_id must 400."""
    client, _, _ = client_and_sink
    r = await client.put("/api/settings/lark", json={"enabled": True})
    assert r.status_code == 400
    assert "chat_id" in r.json()["detail"] or "user_id" in r.json()["detail"]


@pytest.mark.asyncio
async def test_put_enable_after_target_clear_returns_400(client_and_sink):
    """v1 P0 (dual): existing enabled=True row, PUT clearing chat_id
    must be rejected because merged result has enabled=True + no target."""
    client, _, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_abc")
    r = await client.put("/api/settings/lark", json={"chat_id": ""})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_put_disable_then_clear_target_ok(client_and_sink):
    """Symmetric: disabling + clearing target is fine."""
    client, _, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_abc")
    r = await client.put("/api/settings/lark", json={"enabled": False, "chat_id": ""})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_put_dnd_hours_out_of_range_returns_422(client_and_sink):
    client, _, _ = client_and_sink
    r = await client.put("/api/settings/lark", json={"dnd_hours": [24]})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_invalid_tz_returns_422(client_and_sink):
    client, _, _ = client_and_sink
    r = await client.put("/api/settings/lark", json={"tz": "Not/A/Real/Zone"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_utc_tz_accepted(client_and_sink):
    client, _, _ = client_and_sink
    r = await client.put("/api/settings/lark",
                         json={"chat_id": "oc_a", "enabled": True, "tz": "UTC"})
    assert r.status_code == 200
    assert r.json()["tz"] == "UTC"


@pytest.mark.asyncio
async def test_put_invalid_notification_type_key_returns_422(client_and_sink):
    client, _, _ = client_and_sink
    r = await client.put("/api/settings/lark",
                         json={"enabled_types": {"nonexistent_type": True}})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_put_dedup_window_out_of_range_returns_422(client_and_sink):
    client, _, _ = client_and_sink
    r = await client.put("/api/settings/lark", json={"dedup_window_sec": 0})
    assert r.status_code == 422
    r = await client.put("/api/settings/lark", json={"dedup_window_sec": 999999})
    assert r.status_code == 422


# ---------- PUT: patch semantics + flush ----------
@pytest.mark.asyncio
async def test_put_is_patch_semantics(client_and_sink):
    """Fields absent from the body are left unchanged."""
    client, _, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_abc", dedup_window_sec=120)
    r = await client.put("/api/settings/lark", json={"tz": "UTC"})
    assert r.status_code == 200
    body = r.json()
    assert body["chat_id"] == "oc_abc"  # untouched
    assert body["dedup_window_sec"] == 120  # untouched
    assert body["tz"] == "UTC"  # updated


@pytest.mark.asyncio
async def test_put_enabled_types_merges_not_replaces(client_and_sink):
    client, _, sm = client_and_sink
    await _seed(sm)
    r = await client.put("/api/settings/lark",
                         json={"enabled_types": {"session_crashed": False}})
    assert r.status_code == 200
    body = r.json()
    # Just the one type changed; the others still True
    assert body["enabled_types"]["session_crashed"] is False
    assert body["enabled_types"]["token_warning"] is True
    assert body["enabled_types"]["port_conflict"] is True


@pytest.mark.asyncio
async def test_put_first_enable_seeds_default_types(client_and_sink):
    """When a user PUTs enabled=True on a fresh row that has no
    enabled_types yet, the API auto-seeds the default-True set so the
    sink actually pushes something. Otherwise fresh installs silently
    swallow every notification.

    Default-True set aligns with the fresh-install alembic seed AND the
    d1s3t5u7v9wx backfill migration: 4 legacy PUSH_TYPES + the three
    added post-launch (new_message / auto_needs_review / mission_done).
    """
    client, _, sm = client_and_sink
    r = await client.put(
        "/api/settings/lark",
        json={"enabled": True, "chat_id": "oc_a"},
    )
    assert r.status_code == 200
    body = r.json()
    # Legacy 4
    assert body["enabled_types"]["session_crashed"] is True
    assert body["enabled_types"]["auto_run_failed"] is True
    assert body["enabled_types"]["token_warning"] is True
    assert body["enabled_types"]["port_conflict"] is True
    # New defaults — these are the ones the "why aren't recent messages
    # showing up in Lark?" bug was about. Regression guard.
    assert body["enabled_types"]["new_message"] is True
    assert body["enabled_types"]["auto_needs_review"] is True
    assert body["enabled_types"]["mission_done"] is True


@pytest.mark.asyncio
async def test_put_does_not_reseed_after_explicit_wipe(client_and_sink):
    """Footgun catch: user PUTs enabled_types={} to explicitly wipe,
    then PUTs enabled=True. Prior implementation would re-trigger the
    auto-seed branch because row.enabled_types is falsy again. The
    fix: once the row has been touched with an explicit types payload,
    subsequent PUTs must NOT auto-re-seed."""
    client, _, sm = client_and_sink
    # Seed a working row
    await _seed(sm, enabled=True, chat_id="oc_a")
    # Explicit wipe of enabled_types
    r = await client.put("/api/settings/lark", json={
        "enabled_types": {"session_crashed": False, "auto_run_failed": False,
                          "token_warning": False, "port_conflict": False},
    })
    assert r.status_code == 200
    # User toggles disabled → enabled cycle without touching types
    await client.put("/api/settings/lark", json={"enabled": False})
    r = await client.put("/api/settings/lark", json={"enabled": True})
    body = r.json()
    # The 4 explicitly-set-False types must stay False, NOT resurrect
    assert body["enabled_types"]["session_crashed"] is False
    assert body["enabled_types"]["auto_run_failed"] is False


@pytest.mark.asyncio
async def test_put_dedup_window_change_flushes_sink(client_and_sink):
    """v1 P1: PUT that changes dedup_window_sec must flush the sink cache."""
    client, sink, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_abc", dedup_window_sec=60)
    before = sink.flush_count
    r = await client.put("/api/settings/lark", json={"dedup_window_sec": 10})
    assert r.status_code == 200
    assert sink.flush_count == before + 1


@pytest.mark.asyncio
async def test_put_target_change_flushes_sink(client_and_sink):
    """Changing target invalidates dedup state (measured against old target)."""
    client, sink, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_old")
    before = sink.flush_count
    r = await client.put("/api/settings/lark", json={"chat_id": "oc_new"})
    assert r.status_code == 200
    assert sink.flush_count == before + 1


@pytest.mark.asyncio
async def test_put_unrelated_change_does_not_flush(client_and_sink):
    """Changing tz alone doesn't need a flush (dedup semantics unchanged)."""
    client, sink, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_a")
    before = sink.flush_count
    r = await client.put("/api/settings/lark", json={"tz": "UTC"})
    assert r.status_code == 200
    assert sink.flush_count == before  # no flush


@pytest.mark.asyncio
async def test_put_dnd_hours_dedup_and_sort(client_and_sink):
    client, _, _ = client_and_sink
    r = await client.put("/api/settings/lark",
                         json={"chat_id": "oc_a", "enabled": True,
                               "dnd_hours": [23, 0, 1, 1, 0, 22]})
    assert r.status_code == 200
    assert r.json()["dnd_hours"] == [0, 1, 22, 23]


# ---------- POST /test ----------
@pytest.mark.asyncio
async def test_post_test_returns_success(client_and_sink):
    client, sink, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_a")
    r = await client.post("/api/settings/lark/test")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is True
    assert body["error"] is None
    assert body["duration_ms"] >= 0
    assert sink.send_test_calls == [""]  # called once


@pytest.mark.asyncio
async def test_post_test_returns_400_when_cli_missing(client_and_sink, monkeypatch):
    client, _, _ = client_and_sink
    monkeypatch.setattr(shutil, "which", lambda _: None)
    r = await client.post("/api/settings/lark/test")
    assert r.status_code == 400
    assert "lark-cli" in r.json()["detail"]


@pytest.mark.asyncio
async def test_post_test_reports_error_string(client_and_sink):
    client, sink, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_a")
    sink._send_test_result = (False, "RuntimeError: lark-cli exit 1", 0.02)
    r = await client.post("/api/settings/lark/test")
    body = r.json()
    assert body["sent"] is False
    assert "lark-cli exit 1" in body["error"]


@pytest.mark.asyncio
async def test_post_test_returns_timeout_on_hang(client_and_sink, monkeypatch):
    """v1 P1: /test must not let the frontend hang > 10s. Server-side
    wait_for(8s) → returns sent=False, error='timeout ...' at 8s."""
    import asyncio as _asyncio
    client, sink, sm = client_and_sink
    await _seed(sm, enabled=True, chat_id="oc_a")

    async def slow_send_test(note: str = ""):
        await _asyncio.sleep(20)  # would exceed 8s cap
        return True, None, 20.0

    sink.send_test = slow_send_test  # type: ignore
    # Reduce the timeout for the test so we don't wait 8s
    monkeypatch.setattr("csm.api.lark_settings._TEST_TIMEOUT_SEC", 0.1)

    r = await client.post("/api/settings/lark/test")
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False
    assert "timeout" in body["error"].lower()
