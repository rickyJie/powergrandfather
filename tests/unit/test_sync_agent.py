"""Unit tests for SyncAgent (Phase 2b) — Anthropic call boundary.

Real Anthropic calls are mocked. Covers:
- disabled state (no API key)
- successful decide → SyncDecisionsPayload
- markdown-fence-wrapped JSON tolerated
- schema violation → None + meta.parse_error
- Anthropic exception → None + meta.error
- policy loader roundtrip via in-memory SQLite
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from csm.models import Base
from csm.models.sync_policy import SyncPolicy
from csm.modules.sync.agent import SyncAgent
from csm.utils.time import now_utc_naive
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ---------------------------------------------------------------------------
# In-memory DB fixture with a seeded SyncPolicy row
# ---------------------------------------------------------------------------


@pytest.fixture
async def sm_with_policy():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sm() as db:
        db.add(SyncPolicy(id=1, prompt="TEST_PROMPT", updated_at=now_utc_naive()))
        await db.commit()
    try:
        yield sm
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# Enabled / disabled
# ---------------------------------------------------------------------------


def test_disabled_when_no_api_key():
    a = SyncAgent(sessionmaker=MagicMock(), api_key=None)
    assert a.enabled is False


def test_disabled_when_env_flag_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CSM_SYNC_DISABLED", "1")
    a = SyncAgent(sessionmaker=MagicMock())
    assert a.enabled is False


def test_enabled_with_key_and_no_disable_flag(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("CSM_SYNC_DISABLED", raising=False)
    # Anthropic SDK is installed in the csm env; without it, .enabled would
    # still be True but the client would be None. We only assert enabled here.
    a = SyncAgent(sessionmaker=MagicMock())
    assert a.enabled is True


# ---------------------------------------------------------------------------
# decide() while disabled
# ---------------------------------------------------------------------------


def test_decide_disabled_returns_none_with_hint():
    a = SyncAgent(sessionmaker=MagicMock(), api_key=None)
    payload, meta = asyncio.run(a.decide({"csm_resources": {}}))
    assert payload is None
    assert meta["error"] == "sync_agent_disabled"


# ---------------------------------------------------------------------------
# Successful decide
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_success_parses_payload(sm_with_policy):
    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='{"decisions":['
                                 '{"action":"skip","rationale":"nothing"}'
                                 '],"summary":"ok"}')],
        usage=SimpleNamespace(
            input_tokens=100,
            output_tokens=50,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=80,
        ),
    )
    a = SyncAgent(sessionmaker=sm_with_policy, api_key="sk-test")
    a._client = MagicMock()
    a._client.messages = MagicMock()
    a._client.messages.create = AsyncMock(return_value=fake_resp)

    payload, meta = await a.decide({"csm_resources": {}, "agents": {}})

    assert payload is not None
    assert len(payload.decisions) == 1
    assert payload.decisions[0].action == "skip"
    assert meta["model"] == a._model
    assert meta["prompt_hash"]  # non-empty
    assert meta["token_usage"]["input_tokens"] == 100
    assert meta["token_usage"]["cache_read_input_tokens"] == 80


# ---------------------------------------------------------------------------
# Fence-wrapped JSON tolerated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_tolerates_markdown_fence(sm_with_policy):
    text = (
        '```json\n{"decisions":[{"action":"skip","rationale":"x"}],'
        '"summary":"s"}\n```'
    )
    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=None,
    )
    a = SyncAgent(sessionmaker=sm_with_policy, api_key="sk-test")
    a._client = MagicMock()
    a._client.messages = MagicMock()
    a._client.messages.create = AsyncMock(return_value=fake_resp)
    payload, meta = await a.decide({})
    assert payload is not None
    assert len(payload.decisions) == 1
    assert "parse_error" not in meta


# ---------------------------------------------------------------------------
# Schema violation → None + parse_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_schema_violation_returns_none_with_parse_error(
    sm_with_policy,
):
    text = '{"decisions":[{"action":"delete_from_csm","rationale":"bad"}],' \
           '"summary":"x"}'
    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=None,
    )
    a = SyncAgent(sessionmaker=sm_with_policy, api_key="sk-test")
    a._client = MagicMock()
    a._client.messages = MagicMock()
    a._client.messages.create = AsyncMock(return_value=fake_resp)
    payload, meta = await a.decide({})
    assert payload is None
    assert "parse_error" in meta
    assert "pydantic" in meta["parse_error"]


@pytest.mark.asyncio
async def test_decide_non_json_returns_none_with_json_decode_error(
    sm_with_policy,
):
    fake_resp = SimpleNamespace(
        content=[SimpleNamespace(type="text",
                                 text="I refuse to output JSON")],
        usage=None,
    )
    a = SyncAgent(sessionmaker=sm_with_policy, api_key="sk-test")
    a._client = MagicMock()
    a._client.messages = MagicMock()
    a._client.messages.create = AsyncMock(return_value=fake_resp)
    payload, meta = await a.decide({})
    assert payload is None
    assert "json_decode" in meta["parse_error"]


# ---------------------------------------------------------------------------
# Anthropic exception → None + meta.error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decide_anthropic_error_captured_in_meta(sm_with_policy):
    a = SyncAgent(sessionmaker=sm_with_policy, api_key="sk-test")
    a._client = MagicMock()
    a._client.messages = MagicMock()
    a._client.messages.create = AsyncMock(side_effect=RuntimeError("boom"))
    payload, meta = await a.decide({})
    assert payload is None
    assert meta["error"].startswith("anthropic_error:")
    assert "boom" in meta["error"]


# ---------------------------------------------------------------------------
# Policy loader roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_policy_prompt_reads_row_and_hashes(sm_with_policy):
    a = SyncAgent(sessionmaker=sm_with_policy, api_key="sk-test")
    prompt, prompt_hash = await a.load_policy_prompt()
    assert prompt == "TEST_PROMPT"
    assert len(prompt_hash) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_load_policy_prompt_raises_when_row_missing():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, class_=AsyncSession)
    a = SyncAgent(sessionmaker=sm, api_key="sk-test")
    with pytest.raises(RuntimeError, match="sync_policy"):
        await a.load_policy_prompt()
    await engine.dispose()
