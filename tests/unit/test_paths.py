"""Unit tests for `csm.core.paths`.

Focus on the M4 fix: empty-string env-var must NOT be treated as "set",
because it would silently fall back to the real `~/.claude` and punch
through the sandbox guard. Also covers whitespace-only values, `expanduser`
handling, and the sandbox marker.
"""
from __future__ import annotations

from pathlib import Path

from csm.core import paths

# ---------------------------------------------------------------------------
# M4: empty / whitespace-only env vars must fall back to home
# ---------------------------------------------------------------------------


def test_empty_string_env_falls_back_to_home(monkeypatch):
    """CSM_CLAUDE_HOME='' is the classic misconfig ('export CSM_CLAUDE_HOME=');
    it must NOT be treated as 'sandbox is set' — else the guard is bypassed."""
    monkeypatch.setenv("CSM_CLAUDE_HOME", "")
    assert paths.claude_home() == Path.home() / ".claude"


def test_whitespace_only_env_falls_back_to_home(monkeypatch):
    monkeypatch.setenv("CSM_CODEX_HOME", "   ")
    assert paths.codex_home() == Path.home() / ".codex"


def test_unset_env_falls_back_to_home(monkeypatch):
    monkeypatch.delenv("CSM_CLAUDE_HOME", raising=False)
    monkeypatch.delenv("CSM_CODEX_HOME", raising=False)
    assert paths.claude_home() == Path.home() / ".claude"
    assert paths.codex_home() == Path.home() / ".codex"


# ---------------------------------------------------------------------------
# Env override honored when non-blank
# ---------------------------------------------------------------------------


def test_env_override_honored_when_set(monkeypatch, tmp_path):
    fake = tmp_path / "sandbox_claude"
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(fake))
    assert paths.claude_home() == fake


def test_env_override_expands_user_tilde(monkeypatch):
    monkeypatch.setenv("CSM_CODEX_HOME", "~/some-sandbox")
    result = paths.codex_home()
    # expanduser resolves ~ to the real HOME
    assert str(result).startswith(str(Path.home()))
    assert result.name == "some-sandbox"


# ---------------------------------------------------------------------------
# Derived helpers compose on top of the base helpers
# ---------------------------------------------------------------------------


def test_projects_dir_composes_from_claude_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CSM_CLAUDE_HOME", str(tmp_path / "ch"))
    assert paths.claude_projects_dir() == tmp_path / "ch" / "projects"
    assert paths.claude_settings_file() == tmp_path / "ch" / "settings.json"


def test_sessions_dir_composes_from_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CSM_CODEX_HOME", str(tmp_path / "cx"))
    assert paths.codex_sessions_dir() == tmp_path / "cx" / "sessions"
    assert paths.codex_config_file() == tmp_path / "cx" / "config.toml"


def test_empty_env_also_breaks_derived_helpers(monkeypatch):
    """Regression guard: if the fallback bug ever comes back, derived
    helpers would also point at real ~/.claude/projects — catch it here."""
    monkeypatch.setenv("CSM_CLAUDE_HOME", "")
    assert paths.claude_projects_dir() == Path.home() / ".claude" / "projects"


# ---------------------------------------------------------------------------
# is_sandbox_mode marker
# ---------------------------------------------------------------------------


def test_sandbox_mode_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("CSM_SANDBOX_MODE", raising=False)
    assert paths.is_sandbox_mode() is False


def test_sandbox_mode_flag_on_when_1(monkeypatch):
    monkeypatch.setenv("CSM_SANDBOX_MODE", "1")
    assert paths.is_sandbox_mode() is True


def test_sandbox_mode_flag_off_for_anything_but_1(monkeypatch):
    """Only literal '1' means on. 'true', 'yes', '' all mean off — this is
    intentional (we don't want to guess truthy-value semantics for a
    marker that gates sandbox enforcement)."""
    for val in ["", "0", "true", "yes", "TRUE"]:
        monkeypatch.setenv("CSM_SANDBOX_MODE", val)
        assert paths.is_sandbox_mode() is False, f"unexpected truthy for {val!r}"
