"""Regression tests for the production startup gate."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from pathlib import Path


def test_start_does_not_report_success_before_server_is_reachable(tmp_path: Path) -> None:
    """A delayed bind failure must not pass a one-shot PID liveness check."""
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()

    source = Path(__file__).parents[2] / "scripts" / "start.sh"
    start = scripts / "start.sh"
    shutil.copy2(source, start)

    # The fake process stays alive long enough to fool the old fixed 2-second
    # kill -0 check, but never opens the requested port.
    uvicorn = fake_bin / "uvicorn"
    uvicorn.write_text("#!/usr/bin/env bash\nsleep 5\nexit 1\n")
    uvicorn.chmod(0o755)
    alembic = fake_bin / "alembic"
    alembic.write_text("#!/usr/bin/env bash\nexit 0\n")
    alembic.chmod(0o755)

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "CSM_STARTUP_TIMEOUT_SEC": "1",
    }
    (tmp_path / "home").mkdir()

    try:
        result = subprocess.run(
            ["bash", str(start), "127.0.0.1", "65432"],
            cwd=project,
            env=env,
            text=True,
            capture_output=True,
            timeout=8,
            check=False,
        )
    finally:
        pidfile = project / "csm.pid"
        if pidfile.is_file():
            try:
                os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass

    assert result.returncode != 0
    assert "did not become healthy" in (result.stdout + result.stderr)
    assert not (project / "csm.pid").exists()
