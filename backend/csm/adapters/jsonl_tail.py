"""Incremental tail of Claude Code JSONL log files.

Walks `~/.claude/projects/**/*.jsonl`, remembers (path, last_offset, last_mtime)
per file, and only reads new bytes on each scan. Parsed lines are returned as
raw dicts; converting to domain Events lives in event_stream.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from glob import glob
from pathlib import Path
from typing import Any

from csm.core.jsonl_lines import parse_complete_json_lines

_log = logging.getLogger(__name__)


def project_dir_to_cwd(dirname: str) -> str:
    """Best-effort decode of ~/.claude/projects/<dirname> back to the original cwd.

    Claude Code stores dashes in place of `/`. Real path components containing
    dashes are ambiguous, but for our local-only single-user case this matches
    well enough; downstream callers should treat this as a hint.
    """
    name = dirname.lstrip("-")
    return "/" + name.replace("-", "/")


@dataclass
class RawRecord:
    """Single parsed JSONL line plus traceability metadata."""
    jsonl_path: str
    claude_session_id: str        # jsonl basename without extension
    project_path: str             # decoded cwd
    line_no: int                  # 1-based within the file
    byte_offset: int              # offset of the START of this line
    obj: dict[str, Any]           # the parsed JSON


@dataclass
class _FileMemState:
    offset: int = 0
    mtime: float = 0.0
    line_no: int = 0              # last consumed line number


class JsonlTailer:
    """Incremental tail of every Claude JSONL log under a projects root.

    State (per absolute jsonl path) is held in memory only:
      - `offset`: last consumed byte offset; truncations reset to 0.
      - `mtime`: last observed stat mtime (used by the watchdog for idle).
      - `line_no`: monotonic line counter, useful for traceability.

    `scan_once()` returns parsed records for any new bytes since the last
    call. Important correctness note: only **terminated** lines (ending in
    `\\n` / `\\r`) are consumed — a writer mid-flush leaves the partial line
    in place for the next scan, so we never silently drop a record by
    advancing past its boundary.

    `snapshot()` / `restore()` exist so the offset map can be persisted
    across process restarts via SQLite's `file_state` table.

    `take_newly_seen()` returns and clears the set of paths first observed
    in the latest scan — used by EventStream to emit `session.started` only
    for files that actually contained at least one parseable record (see
    `_tick_once`).
    """

    def __init__(self, projects_root: Path):
        self.projects_root = Path(projects_root)
        self._state: dict[str, _FileMemState] = {}
        self._known_paths: set[str] = set()
        self._newly_seen_paths: set[str] = set()

    # ---- state ----
    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {p: {"offset": s.offset, "mtime": s.mtime, "line_no": s.line_no}
                for p, s in self._state.items()}

    def restore(self, snap: dict[str, dict[str, Any]]) -> None:
        for p, d in snap.items():
            self._state[p] = _FileMemState(offset=d.get("offset", 0),
                                            mtime=d.get("mtime", 0.0),
                                            line_no=d.get("line_no", 0))
            self._known_paths.add(p)

    def take_newly_seen(self) -> set[str]:
        """Return + clear the set of paths first observed in the latest scan."""
        out = self._newly_seen_paths
        self._newly_seen_paths = set()
        return out

    # ---- scan ----
    def scan_once(self) -> list[RawRecord]:
        """Read new bytes from every jsonl under projects_root. Returns parsed records."""
        records: list[RawRecord] = []
        pattern = str(self.projects_root / "**" / "*.jsonl")
        for path in glob(pattern, recursive=True):
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue

            fs = self._state.get(path)
            if fs is None:
                fs = _FileMemState()
                self._state[path] = fs
                if path not in self._known_paths:
                    self._newly_seen_paths.add(path)
                    self._known_paths.add(path)

            # Truncation guard: if file shrank, reset offset to 0.
            if st.st_size < fs.offset:
                fs.offset = 0
                fs.line_no = 0

            if st.st_size == fs.offset:
                # Update mtime even when no new bytes (used by watchdog).
                fs.mtime = st.st_mtime
                continue

            session_id = Path(path).stem
            project_dir = Path(path).parent.name
            cwd = project_dir_to_cwd(project_dir)

            try:
                with open(path, "rb") as f:
                    f.seek(fs.offset)
                    buf = f.read()
            except OSError:
                continue

            parsed = parse_complete_json_lines(buf, fs.offset)
            base_line_no = fs.line_no
            for jl in parsed.lines:
                records.append(RawRecord(
                    jsonl_path=path,
                    claude_session_id=session_id,
                    project_path=cwd,
                    line_no=base_line_no + jl.line_index,
                    byte_offset=jl.byte_offset,
                    obj=jl.obj,
                ))
            fs.line_no = base_line_no + parsed.non_blank_count
            fs.offset = parsed.last_complete_end
            fs.mtime = st.st_mtime

        return records

    def file_states(self) -> dict[str, _FileMemState]:
        """Read-only view used by watchdog for idle / crashed inference."""
        return self._state


# ============================================================================
# Codex rollout tailer (P4 — multi-CLI branch)
# ============================================================================


@dataclass
class CodexRawRecord:
    """Single parsed rollout JSONL line + traceability. Parallel to RawRecord
    but with codex-specific fields — codex_session_id comes from the
    session_meta record (line 0), not from the path stem.
    """
    rollout_path: str
    codex_session_id: str      # from session_meta.payload.session_id
    project_path: str          # from session_meta.payload.cwd
    model: str | None          # from session_meta.payload.model (bootstrapped)
    line_no: int
    byte_offset: int
    obj: dict[str, Any]


@dataclass
class _CodexFileState:
    offset: int = 0
    mtime: float = 0.0
    line_no: int = 0
    codex_session_id: str = ""   # bootstrapped from line 0
    project_path: str = ""       # bootstrapped from line 0
    model: str = ""              # bootstrapped from line 0 (session_meta.model)
    # True once we've logged the "empty session_id" warning for this file,
    # so we don't spam the log every scan tick.
    warned_empty_id: bool = False


class CodexRolloutTailer:
    """Incremental tail of `<codex_home>/sessions/YYYY/MM/DD/rollout-*.jsonl`.

    Structurally similar to JsonlTailer: memoise (offset, mtime, line_no) per
    file, only consume newline-terminated lines. Key difference: codex
    session_id and cwd are NOT derivable from the path. They live inside the
    first record (`type: session_meta`). We bootstrap them into state the
    first time we see a file, then attach them to every subsequent record.

    Records emitted before the session_meta line (should never happen in
    practice — codex always writes session_meta first) are dropped with a
    log message rather than emitted with empty ids.
    """

    def __init__(self, sessions_root: Path):
        self.sessions_root = Path(sessions_root)
        self._state: dict[str, _CodexFileState] = {}
        self._known_paths: set[str] = set()
        self._newly_seen_paths: set[str] = set()

    # ---- state ----
    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {
            p: {
                "offset": s.offset,
                "mtime": s.mtime,
                "line_no": s.line_no,
                "codex_session_id": s.codex_session_id,
                "project_path": s.project_path,
                "model": s.model,
            }
            for p, s in self._state.items()
        }

    def restore(self, snap: dict[str, dict[str, Any]]) -> None:
        for p, d in snap.items():
            project_path = d.get("project_path", "")
            codex_session_id = d.get("codex_session_id", d.get("session_id", ""))
            # FileState stores the offset and external id, but not cwd. Read
            # the small first record so post-restart events retain project
            # attribution without replaying the entire rollout.
            if not project_path:
                try:
                    with open(p, encoding="utf-8") as f:
                        first = json.loads(f.readline())
                    payload = first.get("payload", {}) if isinstance(first, dict) else {}
                    if isinstance(payload, dict):
                        project_path = str(payload.get("cwd") or "")
                        codex_session_id = codex_session_id or str(
                            payload.get("session_id") or payload.get("id") or ""
                        )
                except (OSError, json.JSONDecodeError):
                    pass
            self._state[p] = _CodexFileState(
                offset=d.get("offset", 0),
                mtime=d.get("mtime", 0.0),
                line_no=d.get("line_no", 0),
                codex_session_id=codex_session_id,
                project_path=project_path,
                model=d.get("model", ""),
            )
            self._known_paths.add(p)

    def take_newly_seen(self) -> set[str]:
        out = self._newly_seen_paths
        self._newly_seen_paths = set()
        return out

    # ---- scan ----
    def scan_once(self) -> list[CodexRawRecord]:
        """Read new bytes from every rollout jsonl under sessions_root."""
        records: list[CodexRawRecord] = []
        # Codex layout is date-nested: YYYY/MM/DD/rollout-*.jsonl
        pattern = str(self.sessions_root / "**" / "rollout-*.jsonl")
        for path in glob(pattern, recursive=True):
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue

            fs = self._state.get(path)
            if fs is None:
                fs = _CodexFileState()
                self._state[path] = fs
                if path not in self._known_paths:
                    self._newly_seen_paths.add(path)
                    self._known_paths.add(path)

            # Truncation guard: codex may rotate a rollout file (rare — new
            # session normally means new file — but not impossible). If the
            # file shrank we also need to forget the bootstrapped session_id
            # and cwd, otherwise the new session's records get mis-attributed
            # to the old session (M2 regression).
            if st.st_size < fs.offset:
                fs.offset = 0
                fs.line_no = 0
                fs.codex_session_id = ""
                fs.project_path = ""
                fs.warned_empty_id = False

            if st.st_size == fs.offset:
                fs.mtime = st.st_mtime
                continue

            try:
                with open(path, "rb") as f:
                    f.seek(fs.offset)
                    buf = f.read()
            except OSError:
                continue

            parsed = parse_complete_json_lines(buf, fs.offset)
            base_line_no = fs.line_no
            for jl in parsed.lines:
                obj = jl.obj
                start = jl.byte_offset
                line_no = base_line_no + jl.line_index

                # Bootstrap ids from session_meta on line 1.
                if obj.get("type") == "session_meta" and isinstance(
                    obj.get("payload"), dict
                ):
                    payload = obj["payload"]
                    if not fs.codex_session_id:
                        fs.codex_session_id = str(
                            payload.get("session_id") or payload.get("id") or ""
                        )
                    if not fs.project_path:
                        fs.project_path = str(payload.get("cwd") or "")

                # Model bootstrap: codex 0.145+ does NOT put model in
                # session_meta — it lives in `turn_context.payload.model`
                # (emitted per-turn since a session can switch models).
                # The first record whose payload carries `model` seeds our
                # cached view; the first successful hit wins and stays
                # (matches "session started with model X" semantics —
                # subsequent turn switches aren't reflected in USAGE
                # attribution today; deferred until we see it matters).
                _payload_generic = obj.get("payload")
                if isinstance(_payload_generic, dict):
                    _m = _payload_generic.get("model")
                    if _m and not fs.model:
                        fs.model = str(_m)

                if not fs.codex_session_id:
                    # session_meta had an empty / missing session_id, or
                    # arrived after other records. Skip so we don't emit
                    # empty-id records, but log once per file so operators
                    # can see the whole rollout is going dark (M1 fix —
                    # previously this was a silent black hole).
                    if not fs.warned_empty_id:
                        _log.warning(
                            "codex rollout %s has no session_id yet at line %d "
                            "(record type=%s); dropping records until "
                            "session_meta with a valid id shows up",
                            path,
                            line_no,
                            obj.get("type"),
                        )
                        fs.warned_empty_id = True
                    continue

                records.append(
                    CodexRawRecord(
                        rollout_path=path,
                        codex_session_id=fs.codex_session_id,
                        project_path=fs.project_path,
                        model=fs.model or None,
                        line_no=line_no,
                        byte_offset=start,
                        obj=obj,
                    )
                )

            fs.line_no = base_line_no + parsed.non_blank_count
            fs.offset = parsed.last_complete_end
            fs.mtime = st.st_mtime

        return records

    def file_states(self) -> dict[str, _CodexFileState]:
        return self._state
