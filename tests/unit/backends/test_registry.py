"""Unit tests for `AdapterRegistry`.

Covers:
- register / get / all / names / __contains__ / __len__
- Duplicate name (same instance = no-op; different instance = ValueError)
- Test isolation: separate registry instances don't leak state
- enabled() reads env vars at call time (hot-reload works)
- get() on unknown raises UnknownAgentError with `known` populated
- get_available() combines get() + probe() usability
- statuses() runs probe() on every adapter
"""
from __future__ import annotations

import pytest
from csm.backends.base import Capability, CLIAdapter
from csm.backends.errors import AgentUnavailableError, UnknownAgentError
from csm.backends.registry import AdapterRegistry

from tests.unit.backends._fake_adapter import FakeAdapter, assert_conforms

# ---------------------------------------------------------------------------
# Protocol conformance smoke
# ---------------------------------------------------------------------------


def test_fake_adapter_conforms_to_protocol():
    assert_conforms(FakeAdapter())


def test_fake_adapter_isinstance_check():
    """runtime_checkable Protocol supports isinstance()."""
    assert isinstance(FakeAdapter(), CLIAdapter)


# ---------------------------------------------------------------------------
# register / get / all
# ---------------------------------------------------------------------------


def test_register_via_constructor():
    r = AdapterRegistry([FakeAdapter("a"), FakeAdapter("b")])
    assert r.names() == ["a", "b"]
    assert "a" in r
    assert len(r) == 2


def test_register_via_method():
    r = AdapterRegistry()
    r.register(FakeAdapter("a"))
    r.register(FakeAdapter("b"))
    assert r.names() == ["a", "b"]


def test_registration_order_preserved():
    """all() returns adapters in registration order (dict insertion order)."""
    r = AdapterRegistry([
        FakeAdapter("z"), FakeAdapter("a"), FakeAdapter("m")
    ])
    assert [a.name for a in r.all()] == ["z", "a", "m"]


# ---------------------------------------------------------------------------
# Duplicate registration
# ---------------------------------------------------------------------------


def test_register_same_instance_twice_is_noop():
    """Idempotent on identity — safe for lifespan retry / reload."""
    adapter = FakeAdapter("a")
    r = AdapterRegistry([adapter])
    r.register(adapter)  # should not raise
    assert len(r) == 1


def test_register_different_instance_same_name_raises():
    """Two instances w/ same name = programmer bug, fail loud."""
    r = AdapterRegistry([FakeAdapter("a")])
    with pytest.raises(ValueError, match="already registered"):
        r.register(FakeAdapter("a"))


# ---------------------------------------------------------------------------
# Test isolation — the key thing module-level dicts would break
# ---------------------------------------------------------------------------


def test_two_registry_instances_are_isolated():
    """Backend-review P1: registry must NOT be a module-global.

    A mock adapter registered in registry A must not leak into registry B.
    This test would fail if we regressed to `_ADAPTERS: dict = {}` in
    registry.py.
    """
    r1 = AdapterRegistry([FakeAdapter("only-in-r1")])
    r2 = AdapterRegistry([FakeAdapter("only-in-r2")])
    assert "only-in-r1" in r1
    assert "only-in-r1" not in r2
    assert "only-in-r2" in r2
    assert "only-in-r2" not in r1


# ---------------------------------------------------------------------------
# Lookup — get() / get_available()
# ---------------------------------------------------------------------------


def test_get_returns_registered_adapter():
    a = FakeAdapter("claude")
    r = AdapterRegistry([a])
    assert r.get("claude") is a


def test_get_unknown_raises_with_known_list():
    r = AdapterRegistry([FakeAdapter("claude"), FakeAdapter("codex")])
    with pytest.raises(UnknownAgentError) as exc:
        r.get("gemini")
    assert exc.value.name == "gemini"
    assert set(exc.value.known) == {"claude", "codex"}


def test_get_available_returns_when_usable():
    r = AdapterRegistry([FakeAdapter("a", installed=True, authenticated=True)])
    got = r.get_available("a")
    assert got.name == "a"


def test_get_available_raises_when_not_installed():
    r = AdapterRegistry([FakeAdapter("a", installed=False, authenticated=True)])
    with pytest.raises(AgentUnavailableError, match="not installed"):
        r.get_available("a")


def test_get_available_raises_when_not_authenticated():
    r = AdapterRegistry([FakeAdapter("a", installed=True, authenticated=False)])
    with pytest.raises(AgentUnavailableError, match="not authenticated"):
        r.get_available("a")


def test_get_available_unknown_raises_unknown_not_unavailable():
    """Unknown vs unavailable = different HTTP codes (400 vs 503)."""
    r = AdapterRegistry()
    with pytest.raises(UnknownAgentError):
        r.get_available("gemini")


# ---------------------------------------------------------------------------
# enabled() — opt-out env-driven feature switch
# ---------------------------------------------------------------------------


def test_registered_adapters_enabled_when_no_env_vars(monkeypatch):
    monkeypatch.delenv("CSM_ENABLE_CLAUDE", raising=False)
    monkeypatch.delenv("CSM_ENABLE_CODEX", raising=False)
    r = AdapterRegistry([FakeAdapter("claude"), FakeAdapter("codex")])
    assert [a.name for a in r.enabled()] == ["claude", "codex"]


def test_enabled_reads_env_at_call_time(monkeypatch):
    """Hot-reload works — flag toggle takes effect without reconstructing registry."""
    r = AdapterRegistry([FakeAdapter("claude"), FakeAdapter("codex")])
    monkeypatch.setenv("CSM_ENABLE_CLAUDE", "1")
    assert {a.name for a in r.enabled()} == {"claude", "codex"}
    monkeypatch.setenv("CSM_ENABLE_CODEX", "1")
    assert {a.name for a in r.enabled()} == {"claude", "codex"}
    monkeypatch.setenv("CSM_ENABLE_CLAUDE", "0")
    assert [a.name for a in r.enabled()] == ["codex"]


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes", "ON"])
def test_enabled_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("CSM_ENABLE_CLAUDE", val)
    r = AdapterRegistry([FakeAdapter("claude")])
    assert len(r.enabled()) == 1


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "  ", "typo"])
def test_enabled_falsy_variants(monkeypatch, val):
    monkeypatch.setenv("CSM_ENABLE_CLAUDE", val)
    r = AdapterRegistry([FakeAdapter("claude")])
    assert r.enabled() == []


# ---------------------------------------------------------------------------
# statuses() — bulk probe
# ---------------------------------------------------------------------------


def test_statuses_probes_every_adapter():
    r = AdapterRegistry([
        FakeAdapter("a", installed=True, authenticated=True),
        FakeAdapter("b", installed=False, authenticated=False),
    ])
    statuses = r.statuses()
    assert len(statuses) == 2
    by_name = {s.name: s for s in statuses}
    assert by_name["a"].usable
    assert not by_name["b"].usable
    assert not by_name["b"].installed


def test_capability_flag_exposed_via_probe():
    caps = frozenset({Capability.HOOKS, Capability.PRE_SPAWN_SESSION_ID})
    a = FakeAdapter("claude", capabilities=caps)
    r = AdapterRegistry([a])
    st = r.statuses()[0]
    assert st.capabilities == caps
