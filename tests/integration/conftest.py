"""Integration test fixtures & safety guards."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True, scope="session")
def _csm_claude_argv_guard():
    """Prevent accidental real-claude spawn in integration tests.

    Sets ``CSM_CLAUDE_ARGV='bash -i'`` if not already set — matches the
    convention documented in the project ``CLAUDE.md``. Without this guard,
    a forgotten env var on CI or a dev shell would spawn the real ``claude``
    CLI on every session-creating test and burn real tokens.
    """
    os.environ.setdefault("CSM_CLAUDE_ARGV", "bash -i")
    yield
