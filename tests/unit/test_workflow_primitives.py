"""Unit tests for csm.modules.workflow.primitives (M8 / T7).

Each of the 6 primitives has at least three cases: success, validation
failure with a reason, and file-not-found. Section-scoping is accepted by
the primitives but not interpreted here — the T8 engine slices upstream —
so we only assert the parameter is callable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from csm.modules.workflow.primitives import (
    CheckResult,
    check_contains_substring,
    check_file_exists,
    check_jsonschema,
    check_min_chars,
    check_regex_match,
    check_required_sections,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def md_file(tmp_path: Path) -> Path:
    p = tmp_path / "design.md"
    p.write_text(
        "# Title\n"
        "\n"
        "Intro paragraph with the magic phrase APPROVED.\n"
        "\n"
        "## Goals\n"
        "\n"
        "Some goal text.\n"
        "\n"
        "## Non-Goals\n"
        "\n"
        "Some non-goal text.\n",
        encoding="utf-8",
    )
    return p


@pytest.fixture
def json_file(tmp_path: Path) -> Path:
    p = tmp_path / "report.json"
    p.write_text(json.dumps({"name": "alice", "score": 42}), encoding="utf-8")
    return p


@pytest.fixture
def empty_file(tmp_path: Path) -> Path:
    p = tmp_path / "empty.md"
    p.write_text("", encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# check_file_exists
# ---------------------------------------------------------------------------


def test_file_exists_pass(md_file: Path) -> None:
    r = check_file_exists(str(md_file))
    assert isinstance(r, CheckResult)
    assert r.passed is True
    assert r.reason == ""


def test_file_exists_empty_fails(empty_file: Path) -> None:
    r = check_file_exists(str(empty_file))
    assert r.passed is False
    assert "empty" in r.reason


def test_file_exists_missing(tmp_path: Path) -> None:
    r = check_file_exists(str(tmp_path / "nope.md"))
    assert r.passed is False
    assert "does not exist" in r.reason


# ---------------------------------------------------------------------------
# check_min_chars
# ---------------------------------------------------------------------------


def test_min_chars_pass(md_file: Path) -> None:
    r = check_min_chars(str(md_file), count=10)
    assert r.passed is True


def test_min_chars_fail(md_file: Path) -> None:
    r = check_min_chars(str(md_file), count=10_000)
    assert r.passed is False
    assert "chars" in r.reason


def test_min_chars_missing(tmp_path: Path) -> None:
    r = check_min_chars(str(tmp_path / "nope.md"), count=10)
    assert r.passed is False
    assert "does not exist" in r.reason


def test_min_chars_accepts_section_arg(md_file: Path) -> None:
    # Section is accepted but not interpreted at this layer — just verify it
    # doesn't crash and behaves like the un-sectioned call.
    r = check_min_chars(str(md_file), count=10, section="Goals")
    assert r.passed is True


# ---------------------------------------------------------------------------
# check_required_sections
# ---------------------------------------------------------------------------


def test_required_sections_pass(md_file: Path) -> None:
    r = check_required_sections(str(md_file), ["Goals", "Non-Goals"])
    assert r.passed is True


def test_required_sections_missing_one(md_file: Path) -> None:
    r = check_required_sections(str(md_file), ["Goals", "Risks"])
    assert r.passed is False
    assert "Risks" in r.reason


def test_required_sections_file_missing(tmp_path: Path) -> None:
    r = check_required_sections(str(tmp_path / "nope.md"), ["Goals"])
    assert r.passed is False
    assert "does not exist" in r.reason


def test_required_sections_matches_any_header_level(tmp_path: Path) -> None:
    p = tmp_path / "deep.md"
    p.write_text("### Goals\n\ndeep header still counts\n", encoding="utf-8")
    r = check_required_sections(str(p), ["Goals"])
    assert r.passed is True


# ---------------------------------------------------------------------------
# check_regex_match
# ---------------------------------------------------------------------------


def test_regex_match_pass(md_file: Path) -> None:
    r = check_regex_match(str(md_file), r"magic\s+phrase\s+\w+")
    assert r.passed is True


def test_regex_match_fail(md_file: Path) -> None:
    r = check_regex_match(str(md_file), r"this-will-not-match-anywhere")
    assert r.passed is False
    assert "not found" in r.reason


def test_regex_match_file_missing(tmp_path: Path) -> None:
    r = check_regex_match(str(tmp_path / "nope.md"), r".+")
    assert r.passed is False
    assert "does not exist" in r.reason


def test_regex_match_invalid_pattern(md_file: Path) -> None:
    r = check_regex_match(str(md_file), r"[unterminated")
    assert r.passed is False
    assert "invalid regex" in r.reason


# ---------------------------------------------------------------------------
# check_jsonschema
# ---------------------------------------------------------------------------


_SCHEMA = {
    "type": "object",
    "required": ["name", "score"],
    "properties": {
        "name": {"type": "string"},
        "score": {"type": "integer", "minimum": 0},
    },
}


def test_jsonschema_pass(json_file: Path) -> None:
    r = check_jsonschema(str(json_file), _SCHEMA)
    assert r.passed is True


def test_jsonschema_violation(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"name": "x", "score": -5}), encoding="utf-8")
    r = check_jsonschema(str(p), _SCHEMA)
    assert r.passed is False
    assert "schema violation" in r.reason


def test_jsonschema_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "garbage.json"
    p.write_text("{not json", encoding="utf-8")
    r = check_jsonschema(str(p), _SCHEMA)
    assert r.passed is False
    assert "invalid JSON" in r.reason


def test_jsonschema_file_missing(tmp_path: Path) -> None:
    r = check_jsonschema(str(tmp_path / "nope.json"), _SCHEMA)
    assert r.passed is False
    assert "does not exist" in r.reason


# ---------------------------------------------------------------------------
# check_contains_substring
# ---------------------------------------------------------------------------


def test_contains_substring_pass(md_file: Path) -> None:
    r = check_contains_substring(str(md_file), "APPROVED")
    assert r.passed is True


def test_contains_substring_fail(md_file: Path) -> None:
    r = check_contains_substring(str(md_file), "REJECTED")
    assert r.passed is False
    assert "not found" in r.reason


def test_contains_substring_file_missing(tmp_path: Path) -> None:
    r = check_contains_substring(str(tmp_path / "nope.md"), "anything")
    assert r.passed is False
    assert "does not exist" in r.reason
