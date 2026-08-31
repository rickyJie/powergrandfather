"""Unit tests for `csm.backends.errors` — small module, small test file."""
from __future__ import annotations

from csm.backends.errors import (
    AgentUnavailableError,
    BackendError,
    UnknownAgentError,
)


def test_unknown_agent_carries_name_and_known_list():
    e = UnknownAgentError("gemini", known=["claude", "codex"])
    assert e.name == "gemini"
    assert e.known == ["claude", "codex"]
    assert "gemini" in str(e)
    assert "claude" in str(e)


def test_unknown_agent_default_known_empty():
    e = UnknownAgentError("x")
    assert e.known == []
    assert "<none registered>" in str(e)


def test_agent_unavailable_carries_reason():
    e = AgentUnavailableError("codex", "auth.json missing")
    assert e.name == "codex"
    assert e.reason == "auth.json missing"
    assert "codex" in str(e)
    assert "auth.json missing" in str(e)


def test_both_are_backend_errors():
    """Common base class lets API layer catch both with a single except."""
    assert isinstance(UnknownAgentError("x"), BackendError)
    assert isinstance(AgentUnavailableError("x", "y"), BackendError)
