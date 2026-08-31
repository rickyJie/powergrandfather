"""Backend adapter package — CLI-agnostic abstraction for CSM.

Public API:
    from csm.backends import (
        CLIAdapter, Capability, AdapterStatus, AdapterArgvResult,
        AdapterRegistry, resolve_agent,
        UnknownAgentError, AgentUnavailableError,
        build_default_registry,
    )

# What lives where

- `base.py`      : Protocol + status/argv dataclasses + Capability enum.
- `registry.py`  : `AdapterRegistry` (instance, not a global module dict).
- `resolver.py`  : `resolve_agent(...)` — the single precedence function.
- `errors.py`    : `UnknownAgentError`, `AgentUnavailableError`.
- `claude/`      : ClaudeAdapter + its tailer, events, hooks.
- `codex/`       : CodexAdapter + its tailer, events, hooks stub.

# Adding a new adapter

1. Create `csm/backends/<name>/adapter.py` implementing `CLIAdapter`.
2. Import it in `build_default_registry()` below and add to the list.
3. Add per-adapter unit tests under `tests/unit/backends/<name>/`.
4. Update `docs/backends/adding_a_new_adapter.md` if the shape shifted.

Registration is intentionally STATIC (a plain Python import list) — this
is a single-user local app, not an enterprise plugin marketplace. Entry
points would add packaging complexity for zero user benefit.
"""
from __future__ import annotations

from csm.backends.base import (
    AdapterArgvResult,
    AdapterStatus,
    Capability,
    CLIAdapter,
)
from csm.backends.errors import (
    AgentUnavailableError,
    BackendError,
    UnknownAgentError,
)
from csm.backends.registry import AdapterRegistry
from csm.backends.resolver import resolve_agent


def build_default_registry() -> AdapterRegistry:
    """Instantiate every known adapter and return a fresh registry.

    Called once from `lifespan()`. Kept out of module-import side-effects
    so tests can construct their own registries (or mix-in mocks) without
    inheriting whatever the app instantiated.

    Import order matches registration order. The first-registered adapter
    is also the default UserPreference seed value on a fresh DB — put the
    most-likely-installed CLI first (currently `claude`).

    `CSM_ENABLE_GEMINI_MOCK=1` opts in a mock GeminiAdapter — see M9.6
    smoke-test module docstring. Zero effect in production.
    """
    import os

    from csm.backends.claude.adapter import ClaudeAdapter
    from csm.backends.codex.adapter import CodexAdapter

    adapters: list[CLIAdapter] = [
        ClaudeAdapter(),
        CodexAdapter(),
    ]

    if os.environ.get("CSM_ENABLE_GEMINI_MOCK", "").strip().lower() in {"1", "true", "yes"}:
        from csm.backends._gemini_mock.adapter import GeminiMockAdapter
        adapters.append(GeminiMockAdapter())

    return AdapterRegistry(adapters)


DEFAULT_AGENT_NAME = "claude"
"""Fallback default when no UserPreference row exists. Also used by
`build_default_registry` as the first-registered adapter — first
registered is what the seed row points at."""


__all__ = [
    # base
    "CLIAdapter",
    "Capability",
    "AdapterStatus",
    "AdapterArgvResult",
    # registry
    "AdapterRegistry",
    "build_default_registry",
    "DEFAULT_AGENT_NAME",
    # resolver
    "resolve_agent",
    # errors
    "BackendError",
    "UnknownAgentError",
    "AgentUnavailableError",
]
