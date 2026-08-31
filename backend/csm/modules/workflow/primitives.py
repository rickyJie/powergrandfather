"""Workflow validation primitives (M8 T7).

Six pure functions that the workflow validation engine composes at runtime.
Each one reads at most one file and returns a `CheckResult`. No side effects,
no LLM calls, no network.

Section-scoping (the optional `section` argument on text-based primitives) is
accepted here but **not** interpreted: the T8 engine slices the markdown and
passes the slice through this layer. Implementing slicing here would duplicate
that logic and couple the primitives to a markdown parser.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import jsonschema
import jsonschema.exceptions


@dataclass
class CheckResult:
    passed: bool
    reason: str  # empty when passed


def _read_text(path: str) -> tuple[str | None, CheckResult | None]:
    """Read a file as UTF-8 text. Returns (content, None) on success or
    (None, failure_result) on any I/O error so callers can short-circuit."""
    if not os.path.isfile(path):
        return None, CheckResult(False, f"file does not exist: {path}")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(), None
    except OSError as e:
        return None, CheckResult(False, f"cannot read {path}: {e}")
    except UnicodeDecodeError as e:
        return None, CheckResult(False, f"not utf-8 text: {path}: {e}")


def check_file_exists(path: str) -> CheckResult:
    """Pass iff `path` is a regular file with size > 0."""
    if not os.path.isfile(path):
        return CheckResult(False, f"file does not exist: {path}")
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return CheckResult(False, f"cannot stat {path}: {e}")
    if size <= 0:
        return CheckResult(False, f"file is empty: {path}")
    return CheckResult(True, "")


def check_min_chars(path: str, count: int, section: str | None = None) -> CheckResult:
    """Pass iff the file's text length is at least `count`.

    `section` is accepted for API stability but not interpreted here — see
    module docstring. The T8 engine handles section extraction upstream.
    """
    del section  # documented above; T8 engine slices upstream
    content, fail = _read_text(path)
    if fail is not None:
        return fail
    assert content is not None
    actual = len(content)
    if actual < count:
        return CheckResult(False, f"{path}: {actual} chars < required {count}")
    return CheckResult(True, "")


def check_required_sections(path: str, sections: list[str]) -> CheckResult:
    """Pass iff every name in `sections` appears as a markdown header
    (`#`, `##`, `###`, …) in the file. Header matching uses
    `^#+\\s+<name>\\s*$` per line, case-sensitive."""
    content, fail = _read_text(path)
    if fail is not None:
        return fail
    assert content is not None
    missing: list[str] = []
    for name in sections:
        pattern = rf"^#+\s+{re.escape(name)}\s*$"
        if not re.search(pattern, content, re.MULTILINE):
            missing.append(name)
    if missing:
        return CheckResult(False, f"{path}: missing sections {missing}")
    return CheckResult(True, "")


def check_regex_match(path: str, pattern: str, section: str | None = None) -> CheckResult:
    """Pass iff `re.search(pattern, content)` returns a match.

    `section` is accepted but not interpreted here — see module docstring.
    """
    del section
    content, fail = _read_text(path)
    if fail is not None:
        return fail
    assert content is not None
    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return CheckResult(False, f"invalid regex {pattern!r}: {e}")
    if compiled.search(content) is None:
        return CheckResult(False, f"{path}: pattern {pattern!r} not found")
    return CheckResult(True, "")


def check_jsonschema(path: str, schema: dict[str, Any]) -> CheckResult:
    """Pass iff the file is valid JSON and validates against `schema`."""
    content, fail = _read_text(path)
    if fail is not None:
        return fail
    assert content is not None
    try:
        instance = json.loads(content)
    except json.JSONDecodeError as e:
        return CheckResult(False, f"{path}: invalid JSON: {e}")
    try:
        jsonschema.validate(instance=instance, schema=schema)
    except jsonschema.exceptions.SchemaError as e:
        return CheckResult(False, f"invalid schema: {e.message}")
    except jsonschema.exceptions.ValidationError as e:
        return CheckResult(False, f"{path}: schema violation: {e.message}")
    return CheckResult(True, "")


def check_contains_substring(path: str, text: str, section: str | None = None) -> CheckResult:
    """Pass iff `text` appears literally in the file's content.

    `section` is accepted but not interpreted here — see module docstring.
    """
    del section
    content, fail = _read_text(path)
    if fail is not None:
        return fail
    assert content is not None
    if text not in content:
        return CheckResult(False, f"{path}: substring {text!r} not found")
    return CheckResult(True, "")
