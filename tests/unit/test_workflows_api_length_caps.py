"""Unit tests for workflow-authoring pydantic length caps (C3 security fix).

The `requirement` / `feedback` / `answers` / `free_text` fields on
`ClarifyBody`, `GenerateBody`, and `EditWithAgentBody` get f-string
spliced into `claude -p` argv. Without a length cap, a 10 MB payload
would explode argv / OOM the subprocess.

These tests exercise ONLY pydantic-level validation — they instantiate
the models directly rather than going through FastAPI, so the tests
don't need the whole app / DB / claude wired up.
"""
from __future__ import annotations

import pytest
from csm.api.workflows import ClarifyBody, EditWithAgentBody, GenerateBody
from pydantic import ValidationError

MAX_LEN = 32768


# ---------------------------------------------------------------------------
# ClarifyBody
# ---------------------------------------------------------------------------


def test_clarify_requirement_at_boundary_accepted():
    """Exactly 32768 chars should pass — inclusive upper bound."""
    body = ClarifyBody(repo_path="/tmp", requirement="a" * MAX_LEN)
    assert len(body.requirement) == MAX_LEN


def test_clarify_requirement_over_32k_rejected():
    """40k chars should be rejected by pydantic (ValidationError → HTTP 422)."""
    with pytest.raises(ValidationError) as exc_info:
        ClarifyBody(repo_path="/tmp", requirement="a" * (MAX_LEN + 8000))
    # confirm the error is specifically about length, not something unrelated
    assert any(
        "at most" in str(e).lower() or "length" in str(e).lower()
        for e in exc_info.value.errors()
    )


def test_clarify_requirement_10mb_rejected():
    """The exact attack payload — 10 MB string — must not slip through."""
    with pytest.raises(ValidationError):
        ClarifyBody(repo_path="/tmp", requirement="x" * (10 * 1024 * 1024))


# ---------------------------------------------------------------------------
# GenerateBody
# ---------------------------------------------------------------------------


def test_generate_requirement_at_boundary_accepted():
    body = GenerateBody(repo_path="/tmp", requirement="a" * MAX_LEN)
    assert len(body.requirement) == MAX_LEN


def test_generate_requirement_over_32k_rejected():
    with pytest.raises(ValidationError):
        GenerateBody(repo_path="/tmp", requirement="a" * (MAX_LEN + 1))


def test_generate_answers_value_over_32k_rejected():
    """Each answer value gets spliced into the prompt — must be capped too."""
    with pytest.raises(ValidationError):
        GenerateBody(
            repo_path="/tmp",
            requirement="ok",
            answers={"q1": "a" * (MAX_LEN + 1)},
        )


def test_generate_answers_value_at_boundary_accepted():
    body = GenerateBody(
        repo_path="/tmp",
        requirement="ok",
        answers={"q1": "a" * MAX_LEN},
    )
    assert len(body.answers["q1"]) == MAX_LEN


def test_generate_free_text_value_over_32k_rejected():
    with pytest.raises(ValidationError):
        GenerateBody(
            repo_path="/tmp",
            requirement="ok",
            free_text={"q1": "b" * (MAX_LEN + 1)},
        )


def test_generate_answers_none_accepted():
    """None (default) must not trip the validator."""
    body = GenerateBody(repo_path="/tmp", requirement="ok")
    assert body.answers is None
    assert body.free_text is None


# ---------------------------------------------------------------------------
# EditWithAgentBody
# ---------------------------------------------------------------------------


def test_edit_feedback_at_boundary_accepted():
    body = EditWithAgentBody(feedback="a" * MAX_LEN)
    assert len(body.feedback) == MAX_LEN


def test_edit_feedback_over_32k_rejected():
    with pytest.raises(ValidationError):
        EditWithAgentBody(feedback="a" * (MAX_LEN + 100))


def test_edit_feedback_10mb_rejected():
    with pytest.raises(ValidationError):
        EditWithAgentBody(feedback="x" * (10 * 1024 * 1024))
