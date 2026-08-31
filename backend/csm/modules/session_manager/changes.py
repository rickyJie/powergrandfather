"""Extract file-editing operations from Claude and Codex transcripts.

Claude records ``Edit`` / ``Write`` / ``MultiEdit`` tool calls under
``~/.claude/projects``. Codex records ``apply_patch`` calls and their
outputs under ``~/.codex/sessions``. Both formats are projected into the
same :class:`EditRecord` shape consumed by the Session Changes API.

**Design**:
  * Source of truth is the JSONL, not the filesystem. If an agent issued
    an edit but the file was later reverted or the target was moved,
    the panel still shows the edit — it records what the agent *did*,
    not what currently exists.
  * MultiEdit is expanded into one EditRecord per sub-edit so the diff
    view can render each one distinctly (a MultiEdit with 8 sub-edits
    is 8 rows in the panel).
  * Write records have `old=None`. The diff renderer treats those as
    "new file" (or full-file replace if a prior Read is visible; that
    heuristic is left to the renderer, not this parser).
  * Timestamps come from the top-level `timestamp` field. If missing
    or unparseable, we fall back to string sort — good enough since
    JSONL is append-only and lines are already in time order.

**Not handled here** (deferred):
  * NotebookEdit tool (rare, notebook-specific format)
  * File mutations performed through shell commands or formatters
  * Reconciling with git — the git-based view is a separate concern
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class EditRecord:
    """One file-editing operation extracted from the transcript."""

    ts: str  # ISO string as-is from JSONL; empty if the line lacked one
    tool: str  # "Edit" | "Write" | "MultiEdit-sub"
    file_path: str
    # None when the transcript does not contain a baseline (Claude Write,
    # Codex move/delete metadata-only records).
    old: str | None
    new: str
    tool_use_id: str = ""  # from message.content[i].id — for anchoring / stable keys
    sub_index: int = 0  # 0 for Edit/Write, 0..N-1 for MultiEdit sub-edits
    # Codex Move patches target the destination path in ``file_path`` and
    # retain the old path here. Claude records leave this unset.
    source_path: str | None = None


@dataclass
class FileSummary:
    """Aggregate per file across a session."""

    path: str
    edit_count: int = 0
    tools: set[str] = field(default_factory=set)
    first_ts: str = ""
    last_ts: str = ""
    additions: int = 0
    deletions: int = 0
    change_kind: str = "modified"


def _line_delta(old: str | None, new: str) -> tuple[int, int]:
    """Return activity-oriented added/deleted line counts for one edit."""
    import difflib

    old_lines = [] if old is None else old.splitlines()
    new_lines = new.splitlines()
    added = deleted = 0
    for line in difflib.ndiff(old_lines, new_lines):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            deleted += 1
    return added, deleted


def _change_kind(tool: str) -> str:
    if tool == "ApplyPatchAdd" or tool == "Write":
        return "added"
    if tool == "ApplyPatchDelete":
        return "deleted"
    if tool == "ApplyPatchMove":
        return "renamed"
    return "modified"


def parse_edits_from_jsonl(jsonl_path: Path) -> list[EditRecord]:
    """Walk one session JSONL, return every Edit/Write/MultiEdit sub-operation.

    Returns records in the order they appear in the file (JSONL is
    append-only so this equals chronological order). Malformed lines
    are skipped with a debug log — a corrupt line should never crash
    the endpoint.
    """
    if not jsonl_path.is_file():
        return []

    out: list[EditRecord] = []
    with jsonl_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                log.debug("changes: skipping malformed jsonl line %d in %s",
                          line_no, jsonl_path.name)
                continue
            # `null`, arrays, numbers etc. are valid JSON but not our shape
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            ts = rec.get("timestamp") or ""
            msg = rec.get("message") or {}
            content = msg.get("content") or []
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name") or ""
                inp = block.get("input") or {}
                if not isinstance(inp, dict):
                    continue
                tool_use_id = block.get("id") or ""
                out.extend(_edits_from_tool_use(name, inp, ts, tool_use_id))
    return out


def parse_codex_edits_from_rollout(
    rollout_path: Path,
    *,
    cwd: str,
) -> list[EditRecord]:
    """Return successful ``apply_patch`` edits from one Codex rollout.

    Codex writes a patch request and its result as separate JSONL records.
    A request alone is not evidence that the filesystem changed, so calls
    are paired by ``call_id`` and only explicit successful results are
    projected. In-flight, failed and malformed patches are ignored.
    """
    try:
        is_file = rollout_path.is_file()
    except OSError:
        return []
    if not is_file:
        return []

    calls: list[tuple[str, str, str]] = []
    outputs: dict[str, object] = {}
    structured_edits: list[EditRecord] = []
    saw_structured_patch_result = False
    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    log.debug(
                        "changes: skipping malformed codex line %d in %s",
                        line_no,
                        rollout_path.name,
                    )
                    continue
                if not isinstance(rec, dict):
                    continue
                record_type = rec.get("type")
                if record_type not in {"response_item", "event_msg"}:
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                payload_type = payload.get("type")
                if (
                    record_type == "event_msg"
                    and payload_type == "patch_apply_end"
                ):
                    # New Codex tool protocol wraps apply_patch inside the
                    # generic `exec` tool. The durable, authoritative result
                    # is this event, which includes success plus structured
                    # per-file diffs. Prefer it over trying to parse JavaScript
                    # from the outer exec request.
                    saw_structured_patch_result = True
                    if payload.get("success") is True:
                        changes = payload.get("changes")
                        if isinstance(changes, dict):
                            structured_edits.extend(
                                _edits_from_codex_change_map(
                                    changes,
                                    ts=rec.get("timestamp") or "",
                                    tool_use_id=str(payload.get("call_id") or ""),
                                    cwd=cwd,
                                )
                            )
                    continue
                if record_type != "response_item":
                    continue
                if payload.get("name") == "apply_patch":
                    call_id = payload.get("call_id") or payload.get("id")
                    patch = _codex_patch_text(payload)
                    if isinstance(call_id, str) and call_id and patch is not None:
                        calls.append((rec.get("timestamp") or "", call_id, patch))
                elif payload_type in {
                    "custom_tool_call_output",
                    "function_call_output",
                }:
                    call_id = payload.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        outputs[call_id] = payload.get("output")
    except OSError:
        return []

    # A rollout using the structured protocol also contains older-looking
    # request envelopes in some Codex versions. Returning both sources would
    # duplicate every edit. Legacy direct apply_patch parsing remains the
    # fallback for rollouts that have no patch_apply_end records at all.
    if saw_structured_patch_result:
        return structured_edits

    edits: list[EditRecord] = []
    for ts, call_id, patch in calls:
        if not _codex_tool_succeeded(outputs.get(call_id)):
            continue
        edits.extend(
            _edits_from_codex_patch(
                patch,
                ts=ts,
                tool_use_id=call_id,
                cwd=cwd,
            )
        )
    return edits


def _codex_patch_text(payload: dict) -> str | None:
    """Read patch text from current and older Codex call envelopes."""
    raw = payload.get("input")
    if isinstance(raw, str):
        return raw

    arguments = payload.get("arguments")
    if not isinstance(arguments, str):
        return None
    if arguments.lstrip().startswith("*** Begin Patch"):
        return arguments
    try:
        decoded = json.loads(arguments)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(decoded, dict):
        return None
    for key in ("patch", "input"):
        value = decoded.get(key)
        if isinstance(value, str):
            return value
    return None


_EXIT_ZERO_RE = re.compile(r"(?:^|\n)Exit code:\s*0(?:\s|$)")


def _codex_tool_succeeded(output: object) -> bool:
    """True only when a Codex tool output explicitly reports success."""
    if isinstance(output, dict):
        return output.get("exit_code") == 0
    if not isinstance(output, str):
        return False
    if "Exit code:" in output:
        return bool(_EXIT_ZERO_RE.search(output))
    return "Success. Updated the following files:" in output


def _edits_from_codex_change_map(
    changes: dict,
    *,
    ts: str,
    tool_use_id: str,
    cwd: str,
) -> list[EditRecord]:
    """Project a structured ``patch_apply_end.changes`` map.

    Current Codex emits absolute paths, but resolving relative paths against
    the session cwd keeps the parser compatible with older/dev builds.
    """
    records: list[EditRecord] = []
    sub_index = 0
    for raw_path, raw_change in changes.items():
        if not isinstance(raw_path, str) or not isinstance(raw_change, dict):
            continue
        source_path = _absolute_edit_path(raw_path, cwd)
        move_path = raw_change.get("move_path")
        destination_path = (
            _absolute_edit_path(move_path, cwd)
            if isinstance(move_path, str) and move_path
            else source_path
        )
        change_type = raw_change.get("type")

        if destination_path != source_path:
            records.append(EditRecord(
                ts=ts,
                tool="ApplyPatchMove",
                file_path=destination_path,
                old=None,
                new="",
                tool_use_id=tool_use_id,
                sub_index=sub_index,
                source_path=source_path,
            ))
            sub_index += 1

        if change_type == "add":
            content = raw_change.get("content")
            if not isinstance(content, str):
                continue
            records.append(EditRecord(
                ts=ts,
                tool="ApplyPatchAdd",
                file_path=destination_path,
                old="",
                new=content,
                tool_use_id=tool_use_id,
                sub_index=sub_index,
            ))
            sub_index += 1
            continue

        if change_type == "delete":
            content = raw_change.get("content")
            records.append(EditRecord(
                ts=ts,
                tool="ApplyPatchDelete",
                file_path=source_path,
                old=content if isinstance(content, str) else None,
                new="",
                tool_use_id=tool_use_id,
                sub_index=sub_index,
            ))
            sub_index += 1
            continue

        unified_diff = raw_change.get("unified_diff")
        if not isinstance(unified_diff, str):
            continue
        hunks = _codex_unified_diff_hunks(unified_diff)
        for old, new in hunks:
            records.append(EditRecord(
                ts=ts,
                tool="ApplyPatch",
                file_path=destination_path,
                old=old,
                new=new,
                tool_use_id=tool_use_id,
                sub_index=sub_index,
                source_path=source_path if destination_path != source_path else None,
            ))
            sub_index += 1
    return records


def _codex_unified_diff_hunks(diff_text: str) -> list[tuple[str, str]]:
    """Extract old/new text pairs from Codex's headerless unified diff."""
    hunks: list[tuple[str, str]] = []
    old_lines: list[str] | None = None
    new_lines: list[str] | None = None

    def flush() -> None:
        nonlocal old_lines, new_lines
        if old_lines is not None and new_lines is not None and old_lines != new_lines:
            hunks.append(("\n".join(old_lines), "\n".join(new_lines)))
        old_lines = new_lines = None

    for line in diff_text.splitlines():
        if line.startswith("@@"):
            flush()
            old_lines, new_lines = [], []
            continue
        if old_lines is None or new_lines is None:
            continue
        if line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+") and not line.startswith("+++ "):
            new_lines.append(line[1:])
        elif line.startswith("-") and not line.startswith("--- "):
            old_lines.append(line[1:])
        elif line.startswith(" "):
            old_lines.append(line[1:])
            new_lines.append(line[1:])
    flush()
    return hunks


_PATCH_FILE_HEADERS = {
    "*** Add File: ": "add",
    "*** Update File: ": "update",
    "*** Delete File: ": "delete",
}


def _patch_file_header(line: str) -> tuple[str, str] | None:
    for prefix, operation in _PATCH_FILE_HEADERS.items():
        if line.startswith(prefix):
            path = line[len(prefix):].strip()
            if path:
                return operation, path
    return None


def _absolute_edit_path(path: str, cwd: str) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return str(candidate)


def _edits_from_codex_patch(
    patch: str,
    *,
    ts: str,
    tool_use_id: str,
    cwd: str,
) -> list[EditRecord]:
    """Parse Codex's ``*** Begin Patch`` format into edit records."""
    lines = patch.splitlines()
    if (
        not lines
        or lines[0].strip() != "*** Begin Patch"
        or lines[-1].strip() != "*** End Patch"
    ):
        return []

    records: list[EditRecord] = []
    i = 1
    sub_index = 0
    while i < len(lines) - 1:
        header = _patch_file_header(lines[i])
        if header is None:
            i += 1
            continue
        operation, raw_path = header
        i += 1

        block: list[str] = []
        while i < len(lines) - 1 and _patch_file_header(lines[i]) is None:
            block.append(lines[i])
            i += 1

        source_path = _absolute_edit_path(raw_path, cwd)
        move_line = next(
            (line for line in block if line.startswith("*** Move to: ")),
            None,
        )
        move_target = (
            move_line[len("*** Move to: "):].strip()
            if move_line is not None
            else ""
        )
        if not move_target:
            move_line = None
        destination_path = (
            _absolute_edit_path(move_target, cwd)
            if move_line
            else source_path
        )

        if operation == "add":
            added = [line[1:] for line in block if line.startswith("+")]
            # Add-file grammar is line based and produces a trailing newline
            # whenever at least one content line is present.
            new = "\n".join(added) + ("\n" if added else "")
            records.append(
                EditRecord(
                    ts=ts,
                    tool="ApplyPatchAdd",
                    file_path=destination_path,
                    old="",
                    new=new,
                    tool_use_id=tool_use_id,
                    sub_index=sub_index,
                )
            )
            sub_index += 1
            continue

        if operation == "delete":
            # Codex's Delete File directive contains the path but not the
            # deleted file body. Keep a visible operation record; the diff
            # renderer explains that prior content is unavailable.
            records.append(
                EditRecord(
                    ts=ts,
                    tool="ApplyPatchDelete",
                    file_path=source_path,
                    old=None,
                    new="",
                    tool_use_id=tool_use_id,
                    sub_index=sub_index,
                )
            )
            sub_index += 1
            continue

        if move_line is not None:
            records.append(
                EditRecord(
                    ts=ts,
                    tool="ApplyPatchMove",
                    file_path=destination_path,
                    old=None,
                    new="",
                    tool_use_id=tool_use_id,
                    sub_index=sub_index,
                    source_path=source_path,
                )
            )
            sub_index += 1

        # Update blocks contain one or more @@ hunks. Context lines belong
        # to both old and new strings; '-' only to old and '+' only to new.
        j = 0
        while j < len(block):
            if not (block[j] == "@@" or block[j].startswith("@@ ")):
                j += 1
                continue
            j += 1
            old_lines: list[str] = []
            new_lines: list[str] = []
            changed = False
            while j < len(block) and not (
                block[j] == "@@" or block[j].startswith("@@ ")
            ):
                line = block[j]
                j += 1
                if line == "*** End of File" or line.startswith("*** Move to: "):
                    continue
                if line.startswith(" "):
                    old_lines.append(line[1:])
                    new_lines.append(line[1:])
                elif line.startswith("-"):
                    old_lines.append(line[1:])
                    changed = True
                elif line.startswith("+"):
                    new_lines.append(line[1:])
                    changed = True
            if not changed:
                continue
            records.append(
                EditRecord(
                    ts=ts,
                    tool="ApplyPatch",
                    file_path=destination_path,
                    old="\n".join(old_lines),
                    new="\n".join(new_lines),
                    tool_use_id=tool_use_id,
                    sub_index=sub_index,
                    source_path=source_path if move_line else None,
                )
            )
            sub_index += 1
    return records


def find_codex_rollout(
    sessions_root: Path,
    *,
    external_session_id: str | None,
    cwd: str,
    started_at: datetime | None = None,
) -> Path | None:
    """Locate a Codex rollout when the Session row lacks a usable path.

    New rows should always carry ``rollout_path``. The external-id lookup
    supports moved homes/stale paths. The timestamp fallback is intentionally
    conservative and exists for legacy rows created before post-spawn binding
    was fixed: a matching-cwd rollout must contain session/user activity from
    ten seconds before to five minutes after the CSM row's start time.

    Lookup order — first hit wins:

    1. **State DB fast-path** (codex 0.145.0+): consult
       ``~/.codex/state_*.sqlite``'s ``threads`` table by id (when
       ``external_session_id`` is known) or by ``cwd`` + ``created_at``
       time window. This is the ONLY path that can recover a row when
       codex has not yet flushed the rollout jsonl to disk (empirical
       lag as of 2026-08-05: ~2 minutes after process start).
    2. **Filesystem walk** (works for every codex version): rglob the
       sessions tree and match by session_meta content.
    """
    from_state_db = _find_codex_rollout_via_state_db(
        sessions_root,
        external_session_id=external_session_id,
        cwd=cwd,
        started_at=started_at,
    )
    if from_state_db is not None:
        return from_state_db

    try:
        paths = list(sessions_root.rglob("rollout-*.jsonl"))
    except OSError:
        return None

    legacy_candidates: list[tuple[float, Path]] = []
    for path in paths:
        meta, activity = _codex_rollout_identity(
            path,
            include_activity=not external_session_id and started_at is not None,
        )
        if meta is None or meta["cwd"] != cwd:
            continue
        if external_session_id:
            if meta["session_id"] == external_session_id:
                return path
            continue
        if started_at is None:
            continue

        start = _as_utc_naive(started_at)
        timestamps = [meta["timestamp"], *activity]
        deltas = [
            (stamp - start).total_seconds()
            for stamp in timestamps
            if stamp is not None
        ]
        eligible = [delta for delta in deltas if -10.0 <= delta <= 300.0]
        if eligible:
            legacy_candidates.append((min(abs(delta) for delta in eligible), path))

    if not legacy_candidates:
        return None
    legacy_candidates.sort(key=lambda item: item[0])
    # Do not guess when two same-cwd rollouts became active effectively at
    # the same time. Returning no Changes is safer than attributing another
    # session's code to this one.
    if (
        len(legacy_candidates) > 1
        and legacy_candidates[1][0] - legacy_candidates[0][0] < 2.0
    ):
        return None
    return legacy_candidates[0][1]


def _find_codex_rollout_via_state_db(
    sessions_root: Path,
    *,
    external_session_id: str | None,
    cwd: str,
    started_at: datetime | None,
) -> Path | None:
    """Recover a rollout path from codex's ``~/.codex/state_*.sqlite``.

    Handles two shapes of Session row:

    - ``external_session_id`` is set → direct ``WHERE id = ?`` lookup
      returns the definitive rollout_path (regardless of cwd, since
      the id is globally unique). Useful when the CSM row remembers
      the codex thread id but never got its `rollout_path` column
      backfilled (typical of pre-fix codex bind timeouts).
    - only ``cwd`` + ``started_at`` are known → search by cwd and
      match rows whose ``created_at`` is inside the ±5min window we
      already use for the JSONL walk. Ties are broken by proximity to
      ``started_at``.

    Returns ``None`` when the state DB doesn't exist, when the row
    exists but its ``rollout_path`` points to a missing file, or when
    two rows in the same cwd landed within 2s of each other (same
    ambiguity guard as the filesystem branch).
    """
    # Local import so pure-JSONL code paths never pay the sqlite3 import
    # cost, and so the adapter's helpers aren't a mandatory dependency
    # for changes.py consumers who never touch codex.
    import sqlite3

    from csm.backends.codex.adapter import (
        _codex_home_from_sessions_root,
        _codex_state_db_paths,
        _open_state_db_ro,
    )

    home = _codex_home_from_sessions_root(sessions_root)
    for db_path in _codex_state_db_paths(home):
        conn = _open_state_db_ro(db_path)
        if conn is None:
            continue
        try:
            if external_session_id:
                cur = conn.execute(
                    "SELECT rollout_path FROM threads WHERE id = ? LIMIT 1",
                    (external_session_id,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    candidate = Path(str(row[0]))
                    if candidate.is_file():
                        return candidate
                # Row absent from this DB — fall through to the next
                # candidate DB (usually there's only one anyway).
                continue

            if started_at is None:
                continue

            start = _as_utc_naive(started_at)
            start_ts = int(start.replace(tzinfo=UTC).timestamp())
            cur = conn.execute(
                "SELECT id, rollout_path, created_at FROM threads "
                "WHERE cwd = ? AND created_at BETWEEN ? AND ?",
                (cwd, start_ts - 10, start_ts + 300),
            )
            rows = [(str(rid), str(rpath or ""), int(created))
                    for rid, rpath, created in cur.fetchall()
                    if rid and rpath]
            if not rows:
                continue
            rows.sort(key=lambda item: abs(item[2] - start_ts))
            # Same ambiguity guard as the filesystem branch — refuse to
            # guess when two threads landed within 2s.
            if len(rows) > 1 and abs(rows[1][2] - rows[0][2]) < 2:
                return None
            candidate = Path(rows[0][1])
            if candidate.is_file():
                return candidate
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return None


def codex_rollout_session_id(path: Path) -> str | None:
    """Return the durable Codex conversation id recorded in a rollout."""
    meta, _activity = _codex_rollout_identity(path, include_activity=False)
    if meta is None:
        return None
    session_id = meta.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _as_utc_naive(parsed)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _codex_rollout_identity(
    path: Path,
    *,
    include_activity: bool,
) -> tuple[dict[str, object] | None, list[datetime]]:
    """Read rollout session metadata and optional user-activity timestamps."""
    meta: dict[str, object] | None = None
    activity: list[datetime] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(rec, dict):
                    continue
                payload = rec.get("payload")
                if not isinstance(payload, dict):
                    continue
                if rec.get("type") == "session_meta" and meta is None:
                    session_id = payload.get("session_id") or payload.get("id")
                    record_cwd = payload.get("cwd")
                    if not isinstance(session_id, str) or not isinstance(record_cwd, str):
                        return None, []
                    meta = {
                        "session_id": session_id,
                        "cwd": record_cwd,
                        "timestamp": _parse_timestamp(
                            payload.get("timestamp") or rec.get("timestamp")
                        ),
                    }
                    if not include_activity:
                        break
                    continue
                if not include_activity:
                    continue
                is_user_message = (
                    rec.get("type") == "event_msg"
                    and payload.get("type") == "user_message"
                ) or (
                    rec.get("type") == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                )
                if is_user_message:
                    stamp = _parse_timestamp(rec.get("timestamp"))
                    if stamp is not None:
                        activity.append(stamp)
    except OSError:
        return None, []
    return meta, activity


def _edits_from_tool_use(
    name: str,
    inp: dict,
    ts: str,
    tool_use_id: str,
) -> list[EditRecord]:
    """Extract EditRecord(s) from a single tool_use block."""
    file_path = inp.get("file_path") or inp.get("path")
    if not isinstance(file_path, str) or not file_path:
        return []

    if name == "Edit":
        old = inp.get("old_string")
        new = inp.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return []
        return [EditRecord(
            ts=ts, tool="Edit", file_path=file_path,
            old=old, new=new, tool_use_id=tool_use_id, sub_index=0,
        )]

    if name == "Write":
        content = inp.get("content")
        if not isinstance(content, str):
            return []
        return [EditRecord(
            ts=ts, tool="Write", file_path=file_path,
            old=None, new=content, tool_use_id=tool_use_id, sub_index=0,
        )]

    if name == "MultiEdit":
        edits = inp.get("edits")
        if not isinstance(edits, list):
            return []
        out: list[EditRecord] = []
        for i, sub in enumerate(edits):
            if not isinstance(sub, dict):
                continue
            old = sub.get("old_string")
            new = sub.get("new_string")
            if not isinstance(old, str) or not isinstance(new, str):
                continue
            out.append(EditRecord(
                ts=ts, tool="MultiEdit-sub", file_path=file_path,
                old=old, new=new, tool_use_id=tool_use_id, sub_index=i,
            ))
        return out

    return []


def summarize_by_file(records: list[EditRecord]) -> list[FileSummary]:
    """Aggregate flat records → per-file summaries, sorted by last_ts desc.

    Most-recently-touched files bubble to the top of the panel so the
    user sees what the agent just changed without scrolling.
    """
    by_path: dict[str, FileSummary] = {}
    for r in records:
        s = by_path.get(r.file_path)
        if s is None:
            s = FileSummary(path=r.file_path, first_ts=r.ts)
            by_path[r.file_path] = s
        s.edit_count += 1
        s.tools.add(r.tool)
        added, deleted = _line_delta(r.old, r.new)
        s.additions += added
        s.deletions += deleted
        kind = _change_kind(r.tool)
        # A later ordinary edit to a newly-created/renamed file should not
        # erase the more informative file-level operation label.
        if s.change_kind == "modified" or kind in {"deleted", "renamed"}:
            s.change_kind = kind
        # First/last: JSONL is time-ordered so first non-empty ts wins for
        # first, every subsequent non-empty ts wins for last. Empty ts is
        # ignored so a malformed record doesn't crush the extrema.
        if r.ts:
            if not s.first_ts:
                s.first_ts = r.ts
            s.last_ts = r.ts

    return sorted(
        by_path.values(),
        key=lambda s: s.last_ts or "",
        reverse=True,
    )


def filter_by_path(records: list[EditRecord], file_path: str) -> list[EditRecord]:
    """Return only records touching `file_path`, order preserved."""
    return [r for r in records if r.file_path == file_path]
