"""Unit tests for `resolve_agent()` — the one-and-only agent precedence function.

Precedence chain:
    explicit > context_default > user_default

Contracts encoded:
- explicit=<invalid> raises UnknownAgentError (per backend-review P1;
  silent fall-through would hide "why is my session on the wrong agent" bugs)
- context_default NOT validated (may be a stale YAML value)
- user_default assumed valid (invariant enforced by preference-write API)
"""
from __future__ import annotations

import pytest
from csm.backends.errors import UnknownAgentError
from csm.backends.registry import AdapterRegistry
from csm.backends.resolver import resolve_agent

from tests.unit.backends._fake_adapter import FakeAdapter


@pytest.fixture
def registry():
    return AdapterRegistry([FakeAdapter("claude"), FakeAdapter("codex")])


# ---------------------------------------------------------------------------
# Basic precedence
# ---------------------------------------------------------------------------


def test_explicit_wins_over_context_default_and_user_default(registry):
    got = resolve_agent(
        explicit="codex",
        context_default="claude",
        user_default="claude",
        registry=registry,
    )
    assert got == "codex"


def test_context_default_wins_over_user_default_when_no_explicit(registry):
    got = resolve_agent(
        explicit=None,
        context_default="codex",
        user_default="claude",
        registry=registry,
    )
    assert got == "codex"


def test_user_default_when_no_explicit_no_context_default(registry):
    got = resolve_agent(
        explicit=None,
        context_default=None,
        user_default="claude",
        registry=registry,
    )
    assert got == "claude"


# ---------------------------------------------------------------------------
# Invalid explicit — per backend-review P1
# ---------------------------------------------------------------------------


def test_invalid_explicit_raises_unknown_agent_error(registry):
    with pytest.raises(UnknownAgentError) as exc:
        resolve_agent(
            explicit="gemini",
            context_default="claude",
            user_default="claude",
            registry=registry,
        )
    assert exc.value.name == "gemini"
    assert set(exc.value.known) == {"claude", "codex"}


def test_invalid_explicit_does_not_fall_through_to_user_default(registry):
    """Silent fall-through was explicitly rejected in the architecture review.

    If a user asks for codex and codex is missing, the correct behavior is
    a 400 error, not "well, we ran claude instead". Fall-through creates
    bugs that look like 'why is my session running the wrong CLI'.
    """
    with pytest.raises(UnknownAgentError):
        resolve_agent(
            explicit="nonexistent",
            context_default=None,
            user_default="claude",
            registry=registry,
        )


# ---------------------------------------------------------------------------
# Context default NOT validated (module docstring contract)
# ---------------------------------------------------------------------------


def test_context_default_pass_through_even_if_unknown(registry):
    """context_default may come from a stale workflow YAML; we don't validate.

    Callers that care about validity should probe separately. This keeps
    resolver a pure function of its 4 inputs.
    """
    got = resolve_agent(
        explicit=None,
        context_default="stale-agent-from-old-yaml",
        user_default="claude",
        registry=registry,
    )
    assert got == "stale-agent-from-old-yaml"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_explicit_empty_string_treated_as_invalid_name(registry):
    """Empty string is a value, not None — must fail explicit-name check.

    Common misconfig: front-end sends `agent: ""` when the field is
    empty. We want that to be a 400, not a silent default-fall-through.
    """
    with pytest.raises(UnknownAgentError):
        resolve_agent(
            explicit="",
            context_default=None,
            user_default="claude",
            registry=registry,
        )


def test_registry_with_only_user_default_still_resolves(registry):
    """User default may be the only registered adapter."""
    small = AdapterRegistry([FakeAdapter("only")])
    got = resolve_agent(
        explicit=None,
        context_default=None,
        user_default="only",
        registry=small,
    )
    assert got == "only"


def test_all_three_none_returns_user_default(registry):
    """user_default is the required floor; can't be None per signature."""
    got = resolve_agent(
        explicit=None,
        context_default=None,
        user_default="claude",
        registry=registry,
    )
    assert got == "claude"
