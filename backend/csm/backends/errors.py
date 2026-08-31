"""Errors raised from the backend adapter layer.

These are separate from HTTP concerns — the API layer catches them and
translates to 400 / 503. Domain code should raise these, not HTTPException,
so tests and internal callers get proper Python exceptions.
"""
from __future__ import annotations


class BackendError(Exception):
    """Base class for all adapter-layer errors."""


class UnknownAgentError(BackendError):
    """Explicit agent name is not registered.

    Raised by `resolver.resolve_agent()` when an explicit override names
    an adapter that doesn't exist. API maps to HTTP 400.
    """

    def __init__(self, name: str, known: list[str] | None = None):
        self.name = name
        self.known = known or []
        known_str = ", ".join(sorted(self.known)) if self.known else "<none registered>"
        super().__init__(
            f"unknown agent {name!r}; registered: {known_str}"
        )


class AgentUnavailableError(BackendError):
    """Adapter is registered but its probe reports it can't run.

    Raised at spawn time when e.g. the CLI binary is missing on PATH or
    auth is not configured. API maps to HTTP 503 with the probe's
    human-readable `error` field.
    """

    def __init__(self, name: str, reason: str):
        self.name = name
        self.reason = reason
        super().__init__(f"agent {name!r} unavailable: {reason}")
