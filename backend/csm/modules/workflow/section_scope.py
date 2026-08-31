"""Markdown section slicing for workflow validation (M8 T8).

Resolves PRD §11 open question #1: a section starts at its header line
(`## Foo`) and ends at the next header line whose depth is **less than or
equal** to the start header's depth, or at end-of-file. Subheadings deeper
than the start (e.g. `### Sub` inside `## Foo`) stay inside the slice.

Used by the validation engine to scope primitives that accept a `section`
argument (`min_chars`, `regex_match`, `contains_substring`). The engine
extracts the slice with this module, writes it to a tmp file, then hands
the tmp path to the same pure primitives that operate on whole files —
this keeps `primitives.py` free of any markdown-parsing logic.
"""
from __future__ import annotations

import re

_HEADER_RE = re.compile(r"^(#+)\s+(.+?)\s*$")


def _parse_header_arg(section_header: str) -> tuple[int, str] | None:
    """Parse `"## Result"` → (2, "Result"). Returns None on malformed input.

    Tolerates trailing whitespace and CRLF; rejects bare titles without `#`
    so the engine surfaces a clear "section not found" rather than matching
    every header in the doc.
    """
    s = section_header.strip()
    m = _HEADER_RE.match(s)
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def _match_header(line: str) -> tuple[int, str] | None:
    """Return (depth, title) if the line is a markdown header, else None."""
    stripped = line.rstrip("\r\n")
    m = _HEADER_RE.match(stripped)
    if not m:
        return None
    return len(m.group(1)), m.group(2).strip()


def extract_section(content: str, section_header: str) -> str | None:
    """Slice `content` to the markdown section identified by `section_header`.

    `section_header` is the literal header (e.g. `"## Result"`); matching is
    case-sensitive on the title and exact on the `#` depth. The returned
    slice includes the header line itself and stops at the line *before*
    the next header of equal-or-shallower depth (depth ≤ start depth).
    EOF terminates the last section.

    Returns None if the header is malformed or never appears in `content`.
    """
    target = _parse_header_arg(section_header)
    if target is None:
        return None
    target_depth, target_title = target

    lines = content.splitlines(keepends=True)

    start_idx: int | None = None
    for i, line in enumerate(lines):
        h = _match_header(line)
        if h is not None and h == (target_depth, target_title):
            start_idx = i
            break
    if start_idx is None:
        return None

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        h = _match_header(lines[j])
        if h is not None and h[0] <= target_depth:
            end_idx = j
            break

    return "".join(lines[start_idx:end_idx])
