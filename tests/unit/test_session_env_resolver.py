"""Unit tests for csm.modules.session_manager.env.

Covers:
- sniff_from_login_shell parses `export NAME=VALUE`, honours the whitelist,
  swallows subprocess failures.
- read_env_file parses KEY=VALUE with quotes / comments, tolerates missing
  files, warns on malformed lines.
- resolve_proxy_env layers file over sniff, records per-var provenance,
  aggregates warnings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from csm.modules.session_manager.env import (
    PROXY_WHITELIST,
    ProxyResolveResult,
    read_env_file,
    resolve_proxy_env,
    sniff_from_login_shell,
)

# ---------- sniff_from_login_shell ---------------------------------------


def _fake_export_output(pairs: dict[str, str]) -> str:
    """Emit `export -p` style output; posix-quotes values with shlex."""
    lines = []
    for k, v in pairs.items():
        # Bash's `export -p` prints `declare -x NAME="value"`; zsh emits
        # `export NAME='value'`. We cover both in the parser, use the zsh
        # form here (single-quoted).
        lines.append(f"export {k}='{v}'")
    return "\n".join(lines) + "\n"


def test_sniff_extracts_whitelisted_vars() -> None:
    output = _fake_export_output(
        {
            "HTTP_PROXY": "http://proxy.local:7890",
            "HTTPS_PROXY": "http://proxy.local:7890",
            "PATH": "/usr/bin:/bin",  # NOT whitelisted → dropped
            "AWS_SECRET_ACCESS_KEY": "hunter2",  # NOT whitelisted → dropped
            "no_proxy": "localhost,127.0.0.1",
        }
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        )
        env, warnings = sniff_from_login_shell("/bin/zsh", timeout=3.0)

    assert env == {
        "HTTP_PROXY": "http://proxy.local:7890",
        "HTTPS_PROXY": "http://proxy.local:7890",
        "no_proxy": "localhost,127.0.0.1",
    }
    assert warnings == []


def test_sniff_accepts_bash_declare_x_form() -> None:
    output = 'declare -x HTTP_PROXY="http://proxy.local:7890"\n'
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=output, stderr=""
        )
        env, _ = sniff_from_login_shell("/bin/bash", timeout=3.0)
    assert env == {"HTTP_PROXY": "http://proxy.local:7890"}


def test_sniff_missing_shell_returns_empty_with_warning() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        env, warnings = sniff_from_login_shell("/nonexistent/shell", timeout=1.0)
    assert env == {}
    assert warnings and "not found" in warnings[0]


def test_sniff_timeout_returns_empty_with_warning() -> None:
    with patch(
        "subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="zsh", timeout=1.0),
    ):
        env, warnings = sniff_from_login_shell("/bin/zsh", timeout=1.0)
    assert env == {}
    assert warnings and "timed out" in warnings[0]


def test_sniff_empty_shell_returns_empty() -> None:
    env, warnings = sniff_from_login_shell("", timeout=1.0)
    assert env == {}
    assert warnings and "unset" in warnings[0]


def test_sniff_nonzero_rc_still_parses_stdout() -> None:
    # e.g. zshrc has a trailing `false` — rc != 0 but export -p output is valid.
    output = _fake_export_output({"HTTPS_PROXY": "http://p:1"})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout=output, stderr=""
        )
        env, warnings = sniff_from_login_shell("/bin/zsh", timeout=1.0)
    assert env == {"HTTPS_PROXY": "http://p:1"}
    assert warnings and "rc=1" in warnings[0]


# ---------- read_env_file ------------------------------------------------


def test_read_env_file_missing_is_silent(tmp_path: Path) -> None:
    env, warnings = read_env_file(tmp_path / "does-not-exist.env")
    assert env == {}
    assert warnings == []


def test_read_env_file_parses_kv_with_quotes_and_comments(tmp_path: Path) -> None:
    f = tmp_path / "proxy.env"
    f.write_text(
        "# proxy config\n"
        "HTTP_PROXY=http://plain.example:7890\n"
        'HTTPS_PROXY="http://quoted.example:7890"\n'
        "NO_PROXY='localhost,127.0.0.1'\n"
        "\n"
        "PATH=/nope   # non-whitelisted, dropped even without a comment\n"
    )
    env, warnings = read_env_file(f)
    assert env == {
        "HTTP_PROXY": "http://plain.example:7890",
        "HTTPS_PROXY": "http://quoted.example:7890",
        "NO_PROXY": "localhost,127.0.0.1",
    }
    assert warnings == []


def test_read_env_file_warns_on_malformed_line(tmp_path: Path) -> None:
    f = tmp_path / "proxy.env"
    f.write_text("HTTP_PROXY=ok\nJUNKLINE\n")
    env, warnings = read_env_file(f)
    assert env == {"HTTP_PROXY": "ok"}
    assert warnings and "malformed" in warnings[0]


# ---------- resolve_proxy_env ---------------------------------------------


def test_resolve_layers_file_over_sniff(tmp_path: Path) -> None:
    sniff_output = _fake_export_output(
        {
            "HTTP_PROXY": "http://from-sniff:1",
            "HTTPS_PROXY": "http://from-sniff:1",
        }
    )
    env_file = tmp_path / "proxy.env"
    env_file.write_text(
        "HTTP_PROXY=http://from-file:2\n"  # overrides sniff
        "NO_PROXY=localhost\n"              # sniff had no NO_PROXY
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sniff_output, stderr=""
        )
        result = resolve_proxy_env(
            auto_sniff=True,
            env_file=env_file,
            shell="/bin/zsh",
            sniff_timeout=1.0,
        )
    assert isinstance(result, ProxyResolveResult)
    assert result.env == {
        "HTTP_PROXY": "http://from-file:2",   # file wins
        "HTTPS_PROXY": "http://from-sniff:1", # sniff-only
        "NO_PROXY": "localhost",              # file-only
    }
    assert result.sources == {
        "HTTP_PROXY": "file",
        "HTTPS_PROXY": "sniff",
        "NO_PROXY": "file",
    }
    assert result.env_file_exists is True
    assert result.sniff_shell == "/bin/zsh"


def test_resolve_skips_sniff_when_disabled(tmp_path: Path) -> None:
    env_file = tmp_path / "proxy.env"
    env_file.write_text("HTTP_PROXY=http://file:1\n")
    with patch("subprocess.run") as mock_run:
        result = resolve_proxy_env(
            auto_sniff=False,
            env_file=env_file,
            shell="/bin/zsh",
            sniff_timeout=1.0,
        )
        mock_run.assert_not_called()
    assert result.env == {"HTTP_PROXY": "http://file:1"}
    assert result.sniff_shell is None


def test_resolve_returns_empty_when_no_sources(tmp_path: Path) -> None:
    result = resolve_proxy_env(
        auto_sniff=False,
        env_file=tmp_path / "missing.env",
        shell="",
        sniff_timeout=1.0,
    )
    assert result.env == {}
    assert result.sources == {}
    assert result.env_file_exists is False


def test_resolve_env_file_none_is_ok(tmp_path: Path) -> None:
    sniff_output = _fake_export_output({"HTTP_PROXY": "http://s:1"})
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sniff_output, stderr=""
        )
        result = resolve_proxy_env(
            auto_sniff=True,
            env_file=None,
            shell="/bin/zsh",
            sniff_timeout=1.0,
        )
    assert result.env == {"HTTP_PROXY": "http://s:1"}
    assert result.env_file_path is None
    assert result.env_file_exists is False


def test_whitelist_covers_upper_and_lower_forms() -> None:
    # Belt-and-suspenders check: any change to the whitelist that drops
    # one of these will break a real user's setup silently.
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        assert name in PROXY_WHITELIST
        assert name.lower() in PROXY_WHITELIST
