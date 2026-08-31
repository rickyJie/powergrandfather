"""M9.6 acceptance test — mock 3rd adapter (gemini) proves the abstraction.

If any of these assertions fails, it means adding a new CLI-adapter to
the backend REQUIRES corresponding frontend changes — the whole point of
the schema-driven refactor is to make that not the case.
"""
from __future__ import annotations

import pytest
from csm.backends import build_default_registry
from csm.backends._gemini_mock.adapter import GeminiMockAdapter


def test_env_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("CSM_ENABLE_GEMINI_MOCK", raising=False)
    r = build_default_registry()
    assert "gemini" not in r
    # Sanity: the real adapters ARE present.
    assert "claude" in r
    assert "codex" in r


def test_env_flag_registers_mock(monkeypatch):
    monkeypatch.setenv("CSM_ENABLE_GEMINI_MOCK", "1")
    r = build_default_registry()
    assert "gemini" in r
    assert isinstance(r.get("gemini"), GeminiMockAdapter)


def test_gemini_mock_probe_is_usable():
    a = GeminiMockAdapter()
    st = a.probe()
    assert st.installed is True
    assert st.authenticated is True
    assert st.usable is True


def test_gemini_mock_has_color_icon_default_argv():
    """The frontend consumes these three fields verbatim — they must exist
    on any adapter that expects to render correctly."""
    a = GeminiMockAdapter()
    assert isinstance(a.color, str) and a.color.startswith("#")
    assert isinstance(a.icon, str) and len(a.icon) == 1
    assert isinstance(a.default_argv(), str) and "gemini" in a.default_argv()


def test_gemini_mock_flags_schema_covers_all_kinds():
    """Sanity: gemini uses select + checkbox + info in one adapter, which
    stresses every rendering path of <AdapterFlagsPanel>."""
    schema = GeminiMockAdapter().flags_schema()
    kinds = {f.kind for f in schema}
    assert "select" in kinds
    assert "checkbox" in kinds
    assert "info" in kinds


def test_gemini_mock_serialises_cleanly():
    """Verify the API layer would emit gemini metadata identically to
    claude/codex — no adapter-name-specific serialisation."""
    from csm.backends.base import flag_to_dict
    for f in GeminiMockAdapter().flags_schema():
        d = flag_to_dict(f)
        assert "kind" in d


def test_gemini_mock_would_not_spawn():
    """Guard against someone accidentally wiring the mock into real
    session creation — its build_argv must raise."""
    with pytest.raises(NotImplementedError):
        GeminiMockAdapter().build_argv(base_argv=["gemini"], cwd="/tmp")
