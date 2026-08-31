"""ClaudeAdapter — the Claude Code CLI adapter.

Implements the CLIAdapter protocol by composing:
  - `JsonlTailer` (still in `csm.adapters.jsonl_tail`; M3 will move it)
  - `derive_claude_events` (this package)
  - `build_hooks_settings_json` (this package)

Session-id lifecycle: PRE_SPAWN. Claude has `--session-id <uuid>`, so we
generate an id in `pre_spawn_session_id()` and bake it into argv in
`build_argv()`. `post_spawn_bind()` is a no-op.

Capabilities: HOOKS + PRE_SPAWN_SESSION_ID + INTERACTIVE_STREAM +
SYNC_MEMORY / SYNC_MCP / SYNC_SKILLS (last three are runtime-probed).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from csm.adapters.jsonl_tail import JsonlTailer, project_dir_to_cwd
from csm.backends import _skill_fs
from csm.backends.base import (
    AdapterArgvResult,
    AdapterStatus,
    Capability,
    CheckboxFlag,
    FlagDescriptor,
    InfoBlock,
    MarkerSyntax,
    ResumeFlag,
    SelectChoice,
    SelectFlag,
)
from csm.backends.claude.events import derive_claude_events
from csm.backends.claude.hooks import build_hooks_settings_json
from csm.core import paths as _paths
from csm.core.events import Event, EventType
from csm.modules.sync.atomic_write import atomic_write_with_hash_guard
from csm.modules.sync.cli_runner import CLIResult, run_cli
from csm.modules.sync.cli_runner import probe_sync_capabilities as _probe_helper
from csm.modules.sync.marker_block import replace_or_append_marker_block

log = logging.getLogger(__name__)

# Above this, or with any newline in it, a message goes to the PTY as a
# bracketed paste rather than as typing — see `frame_pty_input_sequence`.
# Deliberately well below where claude's own paste-detection is thought to
# kick in: crossing over early only means a short message arrives as a small
# paste (which claude renders inline, indistinguishably), while crossing over
# late means it silently fails to submit. The two errors are not symmetric.
_PASTE_THRESHOLD_BYTES = 200


class ClaudeAdapter:
    """CLIAdapter for Anthropic's `claude` CLI (aka Claude Code)."""

    name = "claude"
    display_name = "Claude Code"
    icon = "C"                    # single-char glyph rendered by AgentBadge
    color = "#d97706"             # Anthropic amber
    capabilities = frozenset({
        Capability.PRE_SPAWN_SESSION_ID,
        Capability.HOOKS,
        Capability.INTERACTIVE_STREAM,
        Capability.RESUME_SESSION,
        # SYNC_MEMORY is always true (markdown files, no CLI subcommand
        # to probe). SYNC_MCP / SYNC_SKILLS depend on the installed
        # claude version and appear in `probe_sync_capabilities()` only
        # when the CLI honours `mcp --help` / `skill --help`.
        Capability.SYNC_MEMORY,
    })

    def default_argv(self) -> str:
        return "claude --dangerously-skip-permissions"

    def flags_schema(self) -> list[FlagDescriptor]:
        return [
            InfoBlock(
                kind="info",
                text=(
                    "CSM enforces --dangerously-skip-permissions for managed "
                    "Claude sessions. This policy is shown in Command and is "
                    "not presented as a toggle because it cannot be disabled."
                ),
            ),
            CheckboxFlag(
                kind="checkbox",
                name="continue",
                label="--continue",
                argv_flag="--continue",
                hint="Continue the last conversation in this cwd.",
            ),
            SelectFlag(
                kind="select",
                name="model",
                label="Model",
                argv_flag="--model",
                choices=(
                    SelectChoice(value="", label="default"),
                    SelectChoice(value="sonnet", label="sonnet"),
                    SelectChoice(value="opus", label="opus"),
                    SelectChoice(value="haiku", label="haiku"),
                ),
            ),
            ResumeFlag(
                kind="resume",
                name="resume",
                label="Resume",
                argv_flag="--resume",
                hint="Reopen an existing Claude JSONL transcript.",
            ),
        ]

    def __init__(self) -> None:
        # Tailer is constructed lazily so tests can monkey-patch the paths
        # module before first scan.
        self._tailer: JsonlTailer | None = None
        # msg_count per session_id — gates SESSION_STARTED emission (adapter
        # only fires the START event once a real record has been parsed, so
        # a freshly-touched empty JSONL doesn't produce a false positive).
        self._msg_count: dict[str, int] = {}
        # Newly-seen paths that we've drained from the tailer but for which
        # msg_count is still 0. Kept across ticks so the SESSION_STARTED
        # emit fires on the tick that first produces a real record. Without
        # this, an empty JSONL first-seen on tick N and populated on tick
        # N+1 would silently lose the SESSION_STARTED signal (drained from
        # tailer on N, tailer doesn't re-report on N+1). Fix for a bug
        # inherited from the pre-refactor event_stream code.
        self._pending_start: dict[str, str] = {}   # sid → path

    # ---- lazy tailer construction ----
    def _get_tailer(self) -> JsonlTailer:
        if self._tailer is None:
            self._tailer = JsonlTailer(self.artifact_root())
        return self._tailer

    # ---- environment ----
    def home_dir(self) -> Path:
        return _paths.claude_home()

    def default_home_name(self) -> str:
        return ".claude"

    def auth_file(self) -> Path | None:
        # Claude uses ~/.claude/.credentials.json for token storage; older
        # installs may store nothing on disk (interactive login flow keeps
        # state in the CLI process). We check the presence of `.credentials.json`
        # OR `.claude.json` (older layout) OR treat "binary present" as
        # authenticated-enough for probe purposes.
        return self.home_dir() / ".credentials.json"

    def probe(self) -> AdapterStatus:
        """Cheap CLI presence check.

        Runs `claude --version` with a 2s timeout to differentiate
        installed-but-broken from installed-and-usable. Auth is inferred
        from the presence of `.credentials.json` OR the binary succeeding
        `--version` (which it does even before login).
        """
        binary = shutil.which("claude")
        if binary is None:
            return AdapterStatus(
                name=self.name,
                installed=False,
                authenticated=False,
                error="`claude` binary not on PATH",
                capabilities=self.capabilities,
            )
        version: str | None = None
        try:
            out = subprocess.run(
                [binary, "--version"],
                capture_output=True, text=True, timeout=2,
            )
            if out.returncode == 0:
                version = (out.stdout or out.stderr).strip().splitlines()[0]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return AdapterStatus(
                name=self.name,
                installed=True,
                authenticated=False,
                error=f"`claude --version` failed: {e}",
                capabilities=self.capabilities,
            )
        # Auth check: credentials file OR any `~/.claude/projects/*` history.
        # Latter is a proxy for "user has used claude at least once".
        auth_ok = (self.auth_file().exists()
                   if self.auth_file() else False)
        if not auth_ok:
            # Fallback: does the projects dir have any content?
            projects = _paths.claude_projects_dir()
            auth_ok = projects.exists() and any(projects.iterdir())
        return AdapterStatus(
            name=self.name,
            installed=True,
            authenticated=auth_ok,
            version=version,
            error=None if auth_ok else "no credentials or prior session found",
            capabilities=self.capabilities,
        )

    # ---- session-id lifecycle ----
    def pre_spawn_session_id(self, cwd: str) -> str | None:
        """Claude accepts `--session-id <uuid>` so we generate one up-front.

        The id gets baked into argv by `build_argv()`. Persisting it on
        the Session row means the JSONL at
        `~/.claude/projects/<encoded_cwd>/<uuid>.jsonl` is immediately
        addressable without a post-hoc reconciliation dance.
        """
        return str(uuid.uuid4())

    def post_spawn_bind(self, session_row_id: str, cwd: str):
        """Claude bakes the id pre-spawn; nothing to do post-spawn.
        Returns None so SessionManager skips the DB write."""
        return None

    # ---- argv ----
    def frame_pty_input(self, text: str) -> bytes:
        """Flattened single-write view. Prefer `frame_pty_input_sequence`."""
        return b"".join(self.frame_pty_input_sequence(text))

    def frame_pty_input_sequence(self, text: str) -> list[bytes]:
        """Short single-line text is typed; anything bigger is PASTED.

        `text + CRLF` in one write is correct only while the burst stays small
        enough for the TUI to read it as typing. Past that, claude's own
        paste-detection takes over and the whole read — trailing CR included —
        is inserted into the composer as literal content. Reported symptom:
        several long messages in a row appeared to send with no error and no
        reply, then a later short "hi" submitted all of them at once, because
        "hi" was small enough to be typed and its CR finally hit Enter on the
        accumulated buffer.

        So say explicitly what a real terminal says. `ESC[200~ … ESC[201~` is
        bracketed paste — the exact bytes an emulator sends when you paste, and
        the shape claude already handles (it is what produces the "[Pasted text
        #1 +N lines]" chip). The submit CR then goes in a SEPARATE write so the
        CLI reads it on its own: after `ESC[201~` the paste is unambiguously
        over, and a lone CR can only be Enter. That mirrors a human pasting and
        then pressing Return, which is the best-tested path through the TUI.

        Short single-line messages keep the old one-write path. They work today
        and the blast radius of getting this wrong is every message, not just
        the long ones — so the new path only takes over where the old one is
        already broken.
        """
        body = text.encode("utf-8", errors="replace")
        if "\n" not in text and len(body) < _PASTE_THRESHOLD_BYTES:
            return [body + b"\r\n"]
        return [b"\x1b[200~" + body + b"\x1b[201~", b"\r"]

    def build_argv(
        self,
        base_argv: list[str],
        cwd: str,
        *,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        extra_args: list[str] | None = None,
        hooks_base_url: str | None = None,
        resume_from: str | None = None,
    ) -> AdapterArgvResult:
        """Construct claude argv.

        Non-claude argv[0] (`["bash", "-i"]` in tests) is strict pass-through
        — no flags, no prompt append, matches the intent of `spawners.py`.

        For real `claude` argv[0]:
          - Injects `--session-id <session_id>` unless the caller passed
            `--resume` / `--continue`.
          - Injects `--dangerously-skip-permissions` so the session manager
            is the single source of truth for permission decisions (user
            policy — see feedback_claude_skip_perms.md).
          - Injects `--settings <hooks_json>` when `hooks_base_url` is
            given, so every hook posts back to CSM.
          - Appends `initial_prompt` as trailing positional (claude
            auto-submits it as the first user turn).

        `resume_from` non-empty means the caller wants to reopen an existing
        claude session id via `--resume <id>` (skip pre-generation and
        skip --session-id — those flags are mutually exclusive without
        `--fork-session`).
        """
        argv = list(base_argv)
        if not argv or argv[0] != "claude":
            return AdapterArgvResult(argv=argv)

        # Resume path — session_id contract: caller passes the id to reopen
        # via `resume_from`; we mirror it into result.session_id so the
        # session row still gets a claude_session_id populated.
        if resume_from:
            effective_sid = resume_from
            if "--resume" not in argv and "-r" not in argv:
                argv = [argv[0], "--resume", resume_from] + argv[1:]
        else:
            effective_sid = session_id
            user_resume_flags = {"--resume", "-r", "--continue", "-c"}
            if (effective_sid
                    and "--session-id" not in argv
                    and not user_resume_flags.intersection(argv)):
                argv = [argv[0], "--session-id", effective_sid] + argv[1:]

        # Permission-skip: CSM is single-source-of-truth.
        if "--dangerously-skip-permissions" not in argv:
            argv.append("--dangerously-skip-permissions")

        # Extras (e.g. --disallowedTools Skill) BEFORE prompt.
        if extra_args:
            argv = argv + list(extra_args)

        # Hooks injection — same slice position as pre-refactor (argv[:3] +
        # ["--settings", json] + argv[3:]) to keep byte-identical output for
        # existing tests / manual comparisons.
        if hooks_base_url and "--settings" not in argv:
            hooks_json = build_hooks_settings_json(hooks_base_url)
            argv = argv[:3] + ["--settings", hooks_json] + argv[3:]

        prompt_appended = False
        if initial_prompt:
            argv = argv + [initial_prompt]
            prompt_appended = True

        return AdapterArgvResult(
            argv=argv,
            session_id=effective_sid,
            prompt_appended=prompt_appended,
        )

    # ---- event streaming ----
    def artifact_root(self) -> Path:
        return _paths.claude_projects_dir()

    def artifact_glob(self) -> str:
        return str(self.artifact_root() / "**" / "*.jsonl")

    def scan_events(self) -> list[Event]:
        """Tail JSONLs + derive events + inject SESSION_STARTED for newly-seen.

        Convention: SESSION_STARTED is emitted for a newly-seen JSONL ONLY
        after we've seen at least one parseable record — protects against
        false positives for touched-but-empty files.
        """
        tailer = self._get_tailer()
        records = tailer.scan_once()
        events: list[Event] = []
        # Per-record derivation.
        for r in records:
            self._msg_count[r.claude_session_id] = (
                self._msg_count.get(r.claude_session_id, 0) + 1
            )
            events.extend(derive_claude_events(r))
        # Merge tailer's newly_seen into our pending_start map. Path may
        # already be pending from a prior tick where the file was empty.
        for path in tailer.take_newly_seen():
            sid = Path(path).stem
            self._pending_start[sid] = path
        # Emit SESSION_STARTED for any pending file that now has msg_count>0.
        emitted_sids: list[str] = []
        for sid, path in self._pending_start.items():
            if self._msg_count.get(sid, 0) == 0:
                continue
            project_dir = Path(path).parent.name
            events.append(Event(
                type=EventType.SESSION_STARTED,
                ts=datetime.now(UTC),
                session_id=sid,
                project_path=project_dir_to_cwd(project_dir),
                payload={"jsonl_path": path, "backend": "claude"},
            ))
            emitted_sids.append(sid)
        for sid in emitted_sids:
            self._pending_start.pop(sid, None)
        return events

    def snapshot(self) -> dict[str, Any]:
        return self._get_tailer().snapshot()

    def restore(self, snap: dict[str, Any]) -> None:
        self._get_tailer().restore(snap)

    def tail_states(self) -> list[dict[str, Any]]:
        """Watchdog view. Claude's session id is the JSONL basename
        (stem); project_path is decoded from the parent dir name."""
        out: list[dict[str, Any]] = []
        for path, state in self._get_tailer().file_states().items():
            p = Path(path)
            out.append({
                "path": path,
                "external_session_id": p.stem,
                "project_path": project_dir_to_cwd(p.parent.name),
                "mtime": state.mtime,
            })
        return out

    def take_newly_seen(self) -> set[str]:
        """Left for API parity with the protocol — scan_events already
        drains this internally, so a caller invoking it separately would
        get an empty set. Provided so external observability can query
        newly-seen state between ticks if we ever need to.
        """
        return set()

    def install_hooks(self, project_root: Path, callback_url: str) -> None:
        """No-op — claude hooks are injected via `--settings` in argv,
        not by writing per-project files (see build_argv above).
        This method exists to satisfy the Protocol; the actual work
        happens at spawn time via `build_argv(hooks_base_url=...)`.
        """
        pass

    # ---- multi-agent config sync (P0 v3 · Phase 2.4) -----------------
    # Introspection methods (memory_paths, skills_dir, list_skills) are
    # always safe; mutating methods (write_memory_marker_block, mcp_add,
    # mcp_remove, write_simple_skill, remove_skill) presume the caller
    # (SyncService) already checked the corresponding Capability.

    def memory_paths(self, scope: str) -> list[Path]:
        """Claude memory files.

        - user  : `~/.claude/CLAUDE.md` (single canonical source).
        - project: []  (per-repo CLAUDE.md is authored in the working
                        directory; SyncService writes to `<cwd>/CLAUDE.md`
                        directly rather than through this adapter — the
                        adapter has no notion of "which project").
        """
        if scope == "user":
            return [self.home_dir() / "CLAUDE.md"]
        return []

    def read_memory(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def read_memory_full(self, scope: str) -> str | None:
        """SyncAgent-facing: concatenated memory contents for `scope`.

        Joins the files listed by `memory_paths(scope)` in listed order
        using two newlines as separator. Returns None if the adapter has
        no memory paths for that scope; returns "" if the paths exist
        but every file is empty / missing.
        """
        paths = self.memory_paths(scope)
        if not paths:
            return None
        return "\n\n".join(self.read_memory(p) for p in paths)

    def write_memory_marker_block(
        self, path: Path, marker_id: str, body: str,
    ) -> None:
        """Insert / replace the `csm:start id=<marker_id>` block in `path`.

        Uses B1 atomic_write_with_hash_guard so a concurrent REPL write
        surfaces as ConcurrentWriteDrift (caller records + skips).
        """
        current = self.read_memory(path)
        updated = replace_or_append_marker_block(
            current, self.marker_syntax(), marker_id, body,
        )
        atomic_write_with_hash_guard(path, updated.encode("utf-8"))

    async def mcp_add(
        self,
        name: str,
        *,
        transport: str,
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CLIResult:
        """Add or re-declare an MCP entry via `claude mcp add`.

        Idempotent: first shells `claude mcp list` and short-circuits if
        an entry with the same name already exists (claude's own CLI
        rejects duplicates as of 2026-07). Env values are passed via the
        subprocess environment dict — never argv (B5).
        """
        existing = await self.mcp_list()
        if any(e.get("name") == name for e in existing):
            return CLIResult(
                argv=("claude", "mcp", "add", name, "<skipped: already exists>"),
                returncode=0, stdout="", stderr="", duration_ms=0, timed_out=False,
            )

        argv: list[str] = ["claude", "mcp", "add", name, "--transport", transport]
        if transport == "stdio":
            if not command:
                raise ValueError("stdio transport requires `command`")
            argv += ["--command", command]
            if args:
                argv += ["--"] + list(args)
        else:  # http / sse
            if not url:
                raise ValueError(f"{transport} transport requires `url`")
            argv += ["--url", url]

        merged_env = {**os.environ, **(env or {})}
        return await run_cli(argv, timeout=10.0, env=merged_env)

    async def mcp_remove(self, name: str) -> CLIResult:
        """Remove `name` via `claude mcp remove`.

        Idempotent: if the entry is not present we return a synthetic
        rc=0 result without invoking the CLI (some claude versions
        return non-zero on missing name, spec §3 forbids that).
        """
        existing = await self.mcp_list()
        if not any(e.get("name") == name for e in existing):
            return CLIResult(
                argv=("claude", "mcp", "remove", name, "<already absent>"),
                returncode=0, stdout="", stderr="not found", duration_ms=0, timed_out=False,
            )
        return await run_cli(["claude", "mcp", "remove", name], timeout=10.0)

    async def mcp_list(self) -> list[dict[str, Any]]:
        """Enumerate installed MCP entries.

        Parses the plain-text output of `claude mcp list`; each non-empty
        line is expected to look like `<name>: <transport> [...]`. Silent
        on parse failures — returns whatever we could extract.
        """
        r = await run_cli(["claude", "mcp", "list"], timeout=5.0)
        if not r.ok:
            return []
        entries: list[dict[str, Any]] = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" not in line:
                entries.append({"name": line, "transport": None, "raw": line})
                continue
            name, rest = line.split(":", 1)
            rest = rest.strip()
            transport = rest.split()[0] if rest else None
            entries.append({"name": name.strip(), "transport": transport, "raw": line})
        return entries

    async def list_mcp_servers_full(self) -> list[dict[str, Any]]:
        return await self.mcp_list()

    def lookup_external_title(self, external_id: str) -> str | None:
        """No-op that satisfies the Protocol.

        claude has no separate title store — it writes `custom-title` /
        `ai-title` records into the JSONL transcript, which the tailer
        already handles. EXTERNAL_TITLE is NOT in `self.capabilities`, so
        EventStream never calls this.
        """
        return None

    def skills_dir(self) -> Path | None:
        return self.home_dir() / "skills"

    def list_skills(self) -> list[dict[str, Any]]:
        """List `<name>/SKILL.md` under skills_dir()."""
        return _skill_fs.list_skills(self.skills_dir())

    def list_skills_full(self) -> list[dict[str, Any]]:
        """`list_skills()` plus body_md, file_count and bundle_hash."""
        return _skill_fs.list_skills_full(self.skills_dir())

    def read_skill_bundle(self, name: str) -> dict[str, Any] | None:
        """Read one skill in full — SKILL.md plus every sibling file."""
        return _skill_fs.read_skill_bundle(self.skills_dir(), name)

    def write_simple_skill(self, spec: dict[str, Any]) -> None:
        """DEPRECATED — use `write_skill_bundle()`. Writes SKILL.md only."""
        self.write_skill_bundle({**spec, "files": [], "prune": None})

    def write_skill_bundle(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Materialise the whole skill directory. See the base protocol."""
        return _skill_fs.write_skill_bundle(self.skills_dir(), spec)

    def remove_skill(self, name: str) -> None:
        """Delete `<skills_dir>/<name>/` recursively. Idempotent + path-guarded."""
        _skill_fs.remove_skill(self.skills_dir(), name)

    def marker_syntax(self) -> MarkerSyntax:
        return MarkerSyntax.html_comment()

    async def probe_sync_capabilities(self) -> frozenset[Capability]:
        """Runtime probe: `claude mcp --help` / `claude skill --help`.

        Result MERGES with the statically-declared caps (SYNC_MEMORY is
        always present because memory files are just markdown — no CLI
        subcommand to probe). SYNC_MCP / SYNC_SKILLS only appear if the
        corresponding `--help` returncode is 0.
        """
        caps: set[Capability] = {Capability.SYNC_MEMORY}
        probed = await _probe_helper("claude")
        if "mcp" in probed:
            caps.add(Capability.SYNC_MCP)
        if "skills" in probed:
            caps.add(Capability.SYNC_SKILLS)
        return frozenset(caps)


__all__ = ["ClaudeAdapter"]
