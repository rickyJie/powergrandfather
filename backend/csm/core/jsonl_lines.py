"""Byte-exact JSONL line splitting, shared by every tailer in the codebase.

FOUR copies of this loop existed: `adapters/jsonl_tail.py` had it twice (claude
and codex), and `modules/agent/jsonl_fast_tail.py` had it twice more — once in
the live tailer and once in the history-replay path a few dozen lines above it.
They had already drifted: only one still carried the comment explaining why a
non-terminated line must not be consumed.

(The first attempt at this extraction missed the fourth copy — in a file it had
just edited — and an independent review caught it. `test_jsonl_lines.py`
now asserts no fifth copy grows back, because "grep and fix them all" is
evidently not a thing a careful reader reliably does.)

That comment guards the single invariant this module exists for:

    A trailing line with no "\\n" means the writer is MID-FLUSH. Consuming it
    parses a truncated record and — worse — advances the stored offset past
    bytes that are about to change, so the completed record is never re-read.
    The offset must only ever advance to the end of the last TERMINATED line.

Getting that wrong doesn't raise. It silently drops one record per flush race,
and the loss is invisible until someone counts events against the transcript.

`line_index` deserves a note too: callers persist a running line number, and
they count NON-BLANK lines including ones that fail to parse. The malformed
lines aren't returned (nobody can use them), so the index is carried on each
result rather than being inferrable from the list position.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = ["JsonLine", "ParsedLines", "parse_complete_json_lines"]


@dataclass(frozen=True)
class JsonLine:
    """One successfully decoded record."""

    byte_offset: int
    """Absolute offset of this line's first byte, for traceability."""

    line_index: int
    """1-based position among non-blank lines in this buffer, counting the
    malformed lines that were skipped. Add to a persisted running total."""

    obj: Any
    """Whatever `json.loads` produced — usually a dict, not guaranteed."""


@dataclass(frozen=True)
class ParsedLines:
    lines: list[JsonLine]

    last_complete_end: int
    """Where to store the offset. Never points into a partial line."""

    non_blank_count: int
    """Advance a persisted line counter by this, NOT by `len(lines)` — the
    difference is the malformed lines, which still consumed a line number."""


def parse_complete_json_lines(buf: bytes, start_offset: int) -> ParsedLines:
    """Split `buf` (read starting at `start_offset`) into complete JSON records.

    Stops at the first line lacking a trailing newline and reports
    `last_complete_end` accordingly, so a mid-flush write is picked up whole on
    the next pass instead of being parsed truncated.

    Blank lines advance the cursor but are not counted or returned.
    Undecodable lines are counted (they occupy a line number) but not returned.
    """
    lines: list[JsonLine] = []
    cursor = start_offset
    last_complete_end = start_offset
    non_blank = 0

    for raw_line in buf.splitlines(keepends=True):
        start = cursor
        cursor += len(raw_line)
        if not (raw_line.endswith(b"\n") or raw_line.endswith(b"\r")):
            # Partial line at end of buffer; do not consume. See module docstring.
            break
        last_complete_end = cursor
        line = raw_line.rstrip(b"\n").rstrip(b"\r")
        if not line:
            continue
        non_blank += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        lines.append(JsonLine(byte_offset=start, line_index=non_blank, obj=obj))

    return ParsedLines(
        lines=lines,
        last_complete_end=last_complete_end,
        non_blank_count=non_blank,
    )
