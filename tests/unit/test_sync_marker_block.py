"""Unit tests for the marker-block replace/append helper."""
from __future__ import annotations

from csm.backends.base import MarkerSyntax
from csm.modules.sync.marker_block import (
    remove_marker_block,
    replace_or_append_marker_block,
)

SYNTAX = MarkerSyntax.html_comment()


def test_append_into_empty_file():
    out = replace_or_append_marker_block("", SYNTAX, "test", "body")
    assert out == (
        "<!-- csm:start id=test -->\n"
        "body\n"
        "<!-- csm:end id=test -->\n"
    )


def test_append_after_existing_content_without_trailing_newline():
    orig = "user content"
    out = replace_or_append_marker_block(orig, SYNTAX, "x", "b")
    assert out == (
        "user content\n"
        "\n"
        "<!-- csm:start id=x -->\n"
        "b\n"
        "<!-- csm:end id=x -->\n"
    )


def test_append_after_content_with_single_newline():
    orig = "user content\n"
    out = replace_or_append_marker_block(orig, SYNTAX, "x", "b")
    assert out == (
        "user content\n"
        "\n"
        "<!-- csm:start id=x -->\n"
        "b\n"
        "<!-- csm:end id=x -->\n"
    )


def test_append_after_content_with_blank_trailer():
    orig = "user content\n\n"
    out = replace_or_append_marker_block(orig, SYNTAX, "x", "b")
    assert out.endswith(
        "user content\n\n"
        "<!-- csm:start id=x -->\n"
        "b\n"
        "<!-- csm:end id=x -->\n"
    )


def test_replace_in_place_preserves_surrounding_text():
    orig = (
        "# heading\n"
        "\n"
        "<!-- csm:start id=lint -->\n"
        "old body\n"
        "<!-- csm:end id=lint -->\n"
        "\n"
        "after text\n"
    )
    out = replace_or_append_marker_block(orig, SYNTAX, "lint", "new body")
    assert "new body" in out
    assert "old body" not in out
    assert "# heading" in out
    assert "after text" in out


def test_replace_only_matching_marker_id():
    """Marker id 'a' must not touch marker id 'b'."""
    orig = (
        "<!-- csm:start id=a -->\n"
        "aa\n"
        "<!-- csm:end id=a -->\n"
        "<!-- csm:start id=b -->\n"
        "bb\n"
        "<!-- csm:end id=b -->\n"
    )
    out = replace_or_append_marker_block(orig, SYNTAX, "a", "AA")
    assert "AA" in out
    assert "bb" in out
    assert "aa" not in out


def test_replace_multiline_body():
    orig = ""
    body = "line1\nline2\nline3"
    out = replace_or_append_marker_block(orig, SYNTAX, "m", body)
    assert "line1\nline2\nline3" in out
    # Replace with different multiline
    out2 = replace_or_append_marker_block(out, SYNTAX, "m", "just one line")
    assert "just one line" in out2
    assert "line1" not in out2


def test_remove_marker_block_is_idempotent_when_absent():
    orig = "nothing here"
    out = remove_marker_block(orig, SYNTAX, "not-present")
    assert out == "nothing here"


def test_remove_marker_block_strips_it():
    orig = (
        "before\n"
        "<!-- csm:start id=x -->\n"
        "middle\n"
        "<!-- csm:end id=x -->\n"
        "after\n"
    )
    out = remove_marker_block(orig, SYNTAX, "x")
    assert "middle" not in out
    assert "before" in out
    assert "after" in out
    assert "\n\n\n" not in out
