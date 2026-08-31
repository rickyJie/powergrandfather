"""The offset invariant three tailers depend on.

`parse_complete_json_lines` replaced three hand-copied versions of this loop.
The one that mattered — never advance the offset into a line the writer is
still flushing — was documented in only one of the three copies, so it is
pinned here rather than left to comment discipline.
"""

import ast
from pathlib import Path

import csm.core.jsonl_lines as jsonl_lines_module
from csm.core.jsonl_lines import parse_complete_json_lines


def test_a_trailing_partial_line_is_left_for_the_next_pass():
    # The writer got half of `{"b": 2}` out before we read.
    buf = b'{"a": 1}\n{"b": 2'
    r = parse_complete_json_lines(buf, 0)
    assert [line.obj for line in r.lines] == [{"a": 1}]
    # Offset stops at the end of the COMPLETE line, not the end of the buffer —
    # otherwise `{"b": 2}` is never read once the writer finishes it.
    assert r.last_complete_end == len(b'{"a": 1}\n')


def test_the_completed_line_is_picked_up_on_the_next_pass():
    first = parse_complete_json_lines(b'{"a": 1}\n{"b": 2', 0)
    # Second pass starts where the first stopped, and the writer has finished.
    rest = b'{"b": 2}\n'
    second = parse_complete_json_lines(rest, first.last_complete_end)
    assert [line.obj for line in second.lines] == [{"b": 2}]
    # No record was lost and none was double-counted.
    assert second.last_complete_end == first.last_complete_end + len(rest)


def test_byte_offsets_are_absolute_not_buffer_relative():
    r = parse_complete_json_lines(b'{"a": 1}\n{"b": 2}\n', 1000)
    assert [line.byte_offset for line in r.lines] == [1000, 1000 + 9]


def test_a_malformed_line_consumes_a_line_number_but_is_not_returned():
    # Callers persist a running line counter; a malformed line still occupied a
    # line, so advancing by len(lines) would silently renumber everything after.
    buf = b'{"a": 1}\nnot json\n{"b": 2}\n'
    r = parse_complete_json_lines(buf, 0)
    assert [line.obj for line in r.lines] == [{"a": 1}, {"b": 2}]
    assert r.non_blank_count == 3
    assert [line.line_index for line in r.lines] == [1, 3]


def test_blank_lines_advance_the_cursor_without_consuming_a_line_number():
    buf = b'{"a": 1}\n\n\n{"b": 2}\n'
    r = parse_complete_json_lines(buf, 0)
    assert r.non_blank_count == 2
    assert [line.line_index for line in r.lines] == [1, 2]
    assert r.last_complete_end == len(buf)


def test_crlf_terminates_a_line():
    r = parse_complete_json_lines(b'{"a": 1}\r\n', 0)
    assert [line.obj for line in r.lines] == [{"a": 1}]
    assert r.last_complete_end == 10


def test_an_empty_buffer_does_not_move_the_offset():
    r = parse_complete_json_lines(b"", 4242)
    assert r.lines == []
    assert r.last_complete_end == 4242
    assert r.non_blank_count == 0


def test_a_buffer_that_is_one_unterminated_line_consumes_nothing():
    r = parse_complete_json_lines(b'{"half": ', 77)
    assert r.lines == []
    assert r.last_complete_end == 77


def test_no_hand_rolled_copy_of_the_loop_grows_back():
    """Rule test: `parse_complete_json_lines` must stay the only implementation.

    The extraction it replaced had FOUR copies, two of them in the same file,
    and they had drifted — one had already lost the comment explaining the
    offset invariant. A reviewer grepping by hand missed one; this does not.

    Matched on the AST, not on substrings: the signature is a `for` that
    iterates `<x>.splitlines(keepends=True)` and calls `json.loads` somewhere
    in its body. Substring matching flagged `api/sessions.py`, which uses
    `splitlines(keepends=True)` for difflib and `json.loads` for something
    unrelated hundreds of lines away — precision matters for a guard that is
    supposed to be trusted rather than muted.
    """
    import csm

    def _is_splitlines_keepends(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "splitlines"
            and any(kw.arg == "keepends" for kw in node.keywords)
        )

    def _calls_json_loads(node: ast.AST) -> bool:
        return any(
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "loads"
            and isinstance(sub.func.value, ast.Name)
            and sub.func.value.id == "json"
            for sub in ast.walk(node)
        )

    root = Path(csm.__file__).resolve().parent
    legitimate = Path(jsonl_lines_module.__file__).resolve()
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or path.resolve() == legitimate:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.For) and _is_splitlines_keepends(node.iter):
                if any(_calls_json_loads(stmt) for stmt in node.body):
                    offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "hand-rolled JSONL line loop found at "
        f"{offenders} — use csm.core.jsonl_lines.parse_complete_json_lines "
        "instead; a private copy will drift from the offset invariant."
    )
