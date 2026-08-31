"""The one and only agent resolution function.

Every spawn call site — Session creation, Workflow stage launch,
AgentDeck conversation, Supervisor review — funnels through
`resolve_agent()`. This is the single point that turns "which CLI should
we use here?" into a concrete adapter name.

Precedence chain (highest to lowest):
    1. `explicit`        — POST body / workflow stage / agent def explicit override
    2. `context_default` — enclosing scope's default (workflow.default_agent, AgentDef.agent)
    3. `user_default`    — UserPreference.default_agent (always present)

Contract:
    - Invalid `explicit` (name not in registry) raises `UnknownAgentError`.
      API layer maps to HTTP 400. Silent fall-through would produce
      "why is this session running claude when I asked for codex" bugs
      that are extremely hard to trace.
    - `context_default` is NOT validated here — it may come from a stale
      workflow YAML or a deleted-then-re-added adapter. Callers that
      want context-default validation must probe separately.
    - `user_default` must always be a registered name (invariant
      enforced by preference write API).
"""
from __future__ import annotations

from csm.backends.errors import UnknownAgentError
from csm.backends.registry import AdapterRegistry


def resolve_agent(
    *,
    explicit: str | None,
    context_default: str | None,
    user_default: str,
    registry: AdapterRegistry,
) -> str:
    """Return the agent name to use, per the precedence chain.

    Raises `UnknownAgentError` if `explicit` is a non-registered name.
    """
    if explicit is not None:
        if explicit not in registry:
            raise UnknownAgentError(explicit, known=registry.names())
        return explicit
    if context_default is not None:
        # Deliberately not validated. See module docstring.
        return context_default
    return user_default


__all__ = ["resolve_agent"]
