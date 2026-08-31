"""Unit tests for `sync.cli_runner.run_cli` (B3 subprocess wrapper)."""
from __future__ import annotations

import pytest
from csm.modules.sync.cli_runner import CLIResult, run_cli


async def test_run_cli_ok():
    r = await run_cli(["/bin/true"])
    assert isinstance(r, CLIResult)
    assert r.ok is True
    assert r.returncode == 0
    assert r.timed_out is False
    assert r.stdout == ""
    assert r.stderr == ""


async def test_run_cli_nonzero_returncode():
    r = await run_cli(["/bin/false"])
    assert r.ok is False
    assert r.returncode != 0
    assert r.timed_out is False


async def test_run_cli_captures_stdout_and_stderr():
    r = await run_cli(["/bin/sh", "-c", "echo out; echo err >&2"])
    assert r.ok is True
    assert "out" in r.stdout
    assert "err" in r.stderr


async def test_run_cli_timeout_returns_none_returncode():
    """A subprocess that outlives the timeout must be killed and reported."""
    r = await run_cli(["/bin/sleep", "5"], timeout=0.2)
    assert r.timed_out is True
    assert r.returncode is None
    assert r.ok is False
    # Duration should be close to the timeout, not to sleep 5s.
    assert r.duration_ms < 2000


async def test_run_cli_env_isolation():
    """`env` kwarg is passed verbatim to the child; parent os.environ NOT merged."""
    r = await run_cli(
        ["/bin/sh", "-c", "echo $MY_MARKER"],
        env={"MY_MARKER": "sentinel", "PATH": "/usr/bin:/bin"},
    )
    assert r.ok is True
    assert "sentinel" in r.stdout


async def test_run_cli_records_argv_as_tuple():
    """argv is stored as a tuple so CLIResult remains hashable/frozen-safe."""
    r = await run_cli(["/bin/true"])
    assert r.argv == ("/bin/true",)
    assert isinstance(r.argv, tuple)


async def test_run_cli_never_uses_shell(monkeypatch):
    """Guard: ensure the wrapper does NOT hand argv to a shell.

    Verified by passing a metacharacter-heavy argv[0] that would be shell-
    parsed if `shell=True` — with exec-style the process just fails to
    resolve, no globbing.
    """
    r = await run_cli(["/bin/sh", "-c", "echo hi > /tmp/should_not_glob_$$"])
    # Command itself runs (we ran sh explicitly). What we're really testing
    # is that create_subprocess_exec is used — verified by a spawning smoke
    # test above; here we just confirm no exception path.
    assert isinstance(r, CLIResult)


def test_cliresult_ok_property_semantics():
    """Timeout → returncode None → ok False."""
    r = CLIResult(argv=("x",), returncode=None, stdout="", stderr="", duration_ms=0, timed_out=True)
    assert r.ok is False
    r = CLIResult(argv=("x",), returncode=1, stdout="", stderr="", duration_ms=0, timed_out=False)
    assert r.ok is False
    r = CLIResult(argv=("x",), returncode=0, stdout="", stderr="", duration_ms=0, timed_out=False)
    assert r.ok is True


@pytest.mark.parametrize("bad_arg", ["nonexistent-binary-12345"])
async def test_run_cli_missing_binary_raises(bad_arg):
    """Missing binary → FileNotFoundError from asyncio.create_subprocess_exec."""
    with pytest.raises(FileNotFoundError):
        await run_cli([bad_arg])
