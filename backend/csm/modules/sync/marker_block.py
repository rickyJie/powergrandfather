"""Marker-block replacement for memory files (CLAUDE.md / AGENTS.md).

CSM owns *fences* — HTML-comment sentinels wrapping an editable body —
inside otherwise user-authored markdown files:

    <!-- csm:start id=python-lint-rules -->
    <body content>
    <!-- csm:end id=python-lint-rules -->

The block is idempotently replaceable by matching `marker_id` on the
outer fences. Content OUTSIDE the fences is preserved byte-for-byte.
"""
from __future__ import annotations

import re

from csm.backends.base import MarkerSyntax


def _fence_line(prefix: str, keyword: str, marker_id: str, suffix: str) -> str:
    """Render one fence line: `<prefix> csm:<keyword> id=<id> <suffix>`."""
    return f"{prefix} csm:{keyword} id={marker_id} {suffix}"


def replace_or_append_marker_block(
    original: str,
    syntax: MarkerSyntax,
    marker_id: str,
    body: str,
) -> str:
    """Return `original` with the marker block for `marker_id` set to `body`.

    - Marker present: replaced in place; surrounding text untouched.
    - Marker absent: appended at end (separated by one blank line iff
      `original` is non-empty and doesn't already end with a newline pair).

    `body` is inserted verbatim between the fences. Callers should pass
    normalized content (trailing newline optional; the function does not
    strip or add one to the body).
    """
    open_line = _fence_line(syntax.open_prefix, "start", marker_id, syntax.open_suffix)
    close_line = _fence_line(syntax.close_prefix, "end", marker_id, syntax.close_suffix)

    # Match: <open_line>\n<body>\n<close_line>
    # Fences are single lines; body can span multiple.
    # We anchor to the exact fence lines to avoid matching unrelated blocks.
    pattern = re.compile(
        rf"{re.escape(open_line)}\n.*?\n{re.escape(close_line)}",
        flags=re.DOTALL,
    )
    new_block = f"{open_line}\n{body}\n{close_line}"

    if pattern.search(original):
        return pattern.sub(new_block, original, count=1)

    # Append with a leading blank-line separator, unless file is empty or
    # already ends with a blank line.
    if not original:
        return new_block + "\n"
    if original.endswith("\n\n"):
        return original + new_block + "\n"
    if original.endswith("\n"):
        return original + "\n" + new_block + "\n"
    return original + "\n\n" + new_block + "\n"


def remove_marker_block(
    original: str,
    syntax: MarkerSyntax,
    marker_id: str,
) -> str:
    """Return `original` with the marker block for `marker_id` removed.

    Idempotent: no-op when the block is absent. Also collapses any
    triple-newline artifact left by the removal.
    """
    open_line = _fence_line(syntax.open_prefix, "start", marker_id, syntax.open_suffix)
    close_line = _fence_line(syntax.close_prefix, "end", marker_id, syntax.close_suffix)

    pattern = re.compile(
        rf"\n?{re.escape(open_line)}\n.*?\n{re.escape(close_line)}\n?",
        flags=re.DOTALL,
    )
    out = pattern.sub("\n", original, count=1)
    # Collapse the seam if we produced a "\n\n\n"; keeps the file tidy.
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    return out


__all__ = ["replace_or_append_marker_block", "remove_marker_block"]
