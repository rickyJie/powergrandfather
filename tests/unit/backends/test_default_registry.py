"""Verify `build_default_registry()` wires both adapters correctly."""
from __future__ import annotations

from csm.backends import (
    DEFAULT_AGENT_NAME,
    build_default_registry,
)


def test_default_registry_has_claude_and_codex():
    r = build_default_registry()
    assert "claude" in r
    assert "codex" in r
    assert len(r) == 2


def test_default_registry_first_registered_is_default_seed():
    """The DEFAULT_AGENT_NAME (seed value for a fresh UserPreference row)
    must be a name that the default registry actually knows about — else
    a first-run user gets an UnknownAgentError on their first spawn."""
    r = build_default_registry()
    assert DEFAULT_AGENT_NAME in r


def test_default_registry_is_fresh_each_call():
    """Two calls produce two independent registries (no shared state)."""
    r1 = build_default_registry()
    r2 = build_default_registry()
    assert r1 is not r2
    # Adapter instances also independent
    assert r1.get("claude") is not r2.get("claude")
