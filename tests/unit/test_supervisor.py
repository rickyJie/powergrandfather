"""Unit tests for SupervisorAgent + onboarding heuristics."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from csm.modules.supervisor.agent import SYSTEM_PROMPT, SupervisorAgent
from csm.modules.supervisor.onboarding import onboarding_warnings

# --- agent ---

@pytest.mark.asyncio
async def test_disabled_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    agent = SupervisorAgent(sessionmaker=MagicMock(), event_stream=MagicMock(), api_key=None)
    assert agent.enabled is False
    # start should be a no-op
    await agent.start()


@pytest.mark.asyncio
async def test_disabled_when_explicitly_off(monkeypatch):
    monkeypatch.setenv("CSM_SUPERVISOR_DISABLED", "1")
    agent = SupervisorAgent(sessionmaker=MagicMock(), event_stream=MagicMock(), api_key="sk-test")
    assert agent.enabled is False


@pytest.mark.asyncio
async def test_ask_claude_parses_json(monkeypatch):
    agent = SupervisorAgent(sessionmaker=MagicMock(), event_stream=MagicMock(), api_key="sk-test")
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(type="text", text='{"needs_review":true,"reason":"err","category":"error"}')]
    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(return_value=fake_resp)
    out = await agent._ask_claude("ctx")
    assert out == {"needs_review": True, "reason": "err", "category": "error"}


@pytest.mark.asyncio
async def test_ask_claude_strips_markdown_fences():
    agent = SupervisorAgent(sessionmaker=MagicMock(), event_stream=MagicMock(), api_key="sk-test")
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(
        type="text",
        text='```json\n{"needs_review":false,"reason":"clean","category":"ok"}\n```',
    )]
    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(return_value=fake_resp)
    out = await agent._ask_claude("ctx")
    assert out["needs_review"] is False


@pytest.mark.asyncio
async def test_ask_claude_uses_caching(monkeypatch):
    agent = SupervisorAgent(sessionmaker=MagicMock(), event_stream=MagicMock(), api_key="sk-test")
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(type="text", text='{"needs_review":false}')]
    agent._client = MagicMock()
    agent._client.messages.create = AsyncMock(return_value=fake_resp)
    await agent._ask_claude("ctx")
    kwargs = agent._client.messages.create.call_args.kwargs
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kwargs["system"][0]["text"] == SYSTEM_PROMPT
    assert kwargs["model"].startswith("claude-")


# --- onboarding ---

def test_onboarding_clean():
    data = {
        "name": "ok",
        "cwd": "/tmp",
        "prompt": "Please do the X work as described in CLAUDE.md, then exit.",
        "output_globs": ["progress.md", "*.json"],
    }
    assert onboarding_warnings(data) == []


def test_onboarding_warns_nonexistent_cwd():
    data = {"name": "x", "cwd": "/nonexistent/path/zz", "prompt": "a" * 30}
    warnings = onboarding_warnings(data)
    assert any("does not exist" in w for w in warnings)


def test_onboarding_warns_short_prompt():
    data = {"name": "x", "cwd": "/tmp", "prompt": "go"}
    warnings = onboarding_warnings(data)
    assert any("very short" in w for w in warnings)


def test_onboarding_warns_bad_glob():
    data = {"name": "x", "cwd": "/tmp", "prompt": "a" * 30, "output_globs": ["/etc/passwd", "../leak"]}
    warnings = onboarding_warnings(data)
    assert sum("absolute or escapes" in w for w in warnings) == 2


def test_onboarding_warns_glob_too_broad():
    data = {"name": "x", "cwd": "/tmp", "prompt": "a" * 30, "output_globs": ["*"]}
    warnings = onboarding_warnings(data)
    assert any("matches everything" in w for w in warnings)


def test_onboarding_warns_undeclared_param_in_prompt():
    data = {
        "name": "x", "cwd": "/tmp",
        "prompt": "do task for group {group_id} iter {n}, more text padding",
        "parameters": [{"name": "group_id"}],
    }
    warnings = onboarding_warnings(data)
    assert any("references ['n']" in w for w in warnings)


def test_onboarding_warns_undeclared_param_in_cwd():
    data = {
        "name": "x", "cwd": "/tmp/{group}/x", "prompt": "a" * 30,
        "parameters": [],
    }
    warnings = onboarding_warnings(data)
    assert any("cwd references" in w for w in warnings)


def test_onboarding_skips_cwd_check_for_template():
    data = {"name": "x", "cwd": "/tmp/{group_id}", "prompt": "a" * 30,
            "parameters": [{"name": "group_id"}]}
    warnings = onboarding_warnings(data)
    # cwd has {group_id} → existence not checked, no warning about it
    assert not any("does not exist" in w for w in warnings)
