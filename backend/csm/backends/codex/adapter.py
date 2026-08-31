"""CodexAdapter — the OpenAI Codex CLI adapter.

Implements the CLIAdapter protocol by composing:
  - `CodexRolloutTailer` (still in `csm.adapters.jsonl_tail`; M3 will move it)
  - `derive_codex_events` (still in `csm.core.codex_events`; M3 will move it)
  - `build_codex_argv` (still in `csm.modules.session_manager.spawners`;
    M3 will move it)

Session-id lifecycle: POST_SPAWN_BIND. Codex has no `--session-id` flag;
the id lives inside the rollout file's first `session_meta` record. So
`pre_spawn_session_id()` returns None, and `post_spawn_bind()` polls the
rollout dir for a newly-created or newly-grown file with matching cwd and
reads the session_id from its first line. Tracking growth matters because
Codex can reopen an existing rollout when continuing a legacy thread.

Capabilities: POST_SPAWN_BIND + INTERACTIVE_STREAM + SYNC_MEMORY (always
true, markdown file). SYNC_MCP and SYNC_SKILLS are added at boot by the
lifespan capability-probe (main.py): SYNC_MCP via `codex mcp --help`, and
SYNC_SKILLS because codex-cli 0.145.0 ships `~/.codex/skills/<name>/SKILL.md`
(directory convention, no `skill` subcommand — see probe_sync_capabilities).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from csm.adapters.jsonl_tail import CodexRolloutTailer
from csm.backends import _skill_fs
from csm.backends.base import (
    AdapterArgvResult,
    AdapterStatus,
    Capability,
    CheckboxFlag,
    FlagDescriptor,
    InfoBlock,
    MarkerSyntax,
    PostSpawnBindResult,
    SelectChoice,
    SelectFlag,
)
from csm.core import paths as _paths
from csm.core.codex_events import derive_codex_events
from csm.core.events import Event
from csm.modules.session_manager.spawners import (
    NO_TRUST_WORKSPACE_FLAG,
    build_codex_argv,
)
from csm.modules.sync.atomic_write import atomic_write_with_hash_guard
from csm.modules.sync.cli_runner import CLIResult, run_cli
from csm.modules.sync.cli_runner import probe_sync_capabilities as _probe_helper
from csm.modules.sync.marker_block import replace_or_append_marker_block

log = logging.getLogger(__name__)


class CodexAdapter:
    """CLIAdapter for OpenAI's `codex` CLI (aka codex-cli)."""

    name = "codex"
    display_name = "Codex CLI"
    icon = "X"  # single-char glyph rendered by AgentBadge
    color = "#0e7490"  # OpenAI teal
    capabilities = frozenset(
        {
            Capability.POST_SPAWN_BIND,
            Capability.INTERACTIVE_STREAM,
            Capability.RESUME_SESSION,
            # codex auto-populates `threads.title` from the first user prompt;
            # see lookup_external_title.
            Capability.EXTERNAL_TITLE,
            # SYNC_MEMORY is always true — codex reads ~/.codex/AGENTS.md
            # (markdown, no CLI subcommand needed). SYNC_MCP and SYNC_SKILLS are
            # added at boot by the lifespan capability-probe (they depend on the
            # installed codex-cli: mcp on `codex mcp --help`, skills on the
            # ~/.codex/skills directory convention). See probe_sync_capabilities.
            Capability.SYNC_MEMORY,
        }
    )

    def default_argv(self) -> str:
        # Match Claude's dangerous-perm-by-default policy: codex sessions
        # spawned from CSM bypass codex's own approval prompt so the
        # session manager is the single source of truth for permission
        # decisions (see global CLAUDE.md rule). `-C` / `-s` are injected
        # by build_codex_argv from `cwd` at spawn time; we surface only
        # the dangerous flag here so users see the argv they'll actually
        # run in the UI's Command field.
        return 'codex --dangerously-bypass-approvals-and-sandbox -c model_reasoning_effort="xhigh"'  # noqa: E501

    def flags_schema(self) -> list[FlagDescriptor]:
        return [
            InfoBlock(
                kind="info",
                text=(
                    "CSM enforces Codex approval bypass for managed sessions. "
                    "The policy is visible in Command and is not shown as a "
                    "toggle because it cannot be disabled."
                ),
            ),
            SelectFlag(
                kind="select",
                name="model",
                label="Model",
                argv_flag="--model",
                # Codex's `-m/--model` accepts any string; codex-cli routes
                # through the configured model_provider. Empty = default
                # (whatever the config.toml or `codex login` picked).
                # List here is the current OpenAI / Codex family of names
                # that show up in rollouts as of 2026-07 — user can still
                # type an arbitrary model in the advanced Command field.
                choices=(
                    SelectChoice(value="", label="default (config.toml)"),
                    SelectChoice(value="gpt-5-codex", label="gpt-5-codex"),
                    SelectChoice(value="gpt-5", label="gpt-5"),
                    SelectChoice(value="gpt-4.1", label="gpt-4.1"),
                    SelectChoice(value="gpt-4o", label="gpt-4o"),
                    SelectChoice(value="gpt-4o-mini", label="gpt-4o-mini"),
                    SelectChoice(value="o3", label="o3"),
                    SelectChoice(value="o3-mini", label="o3-mini"),
                ),
                hint="`-m <name>` — overrides the model in config.toml.",
            ),
            CheckboxFlag(
                kind="checkbox",
                name="no_trust_workspace",
                label="Don't auto-trust this folder",
                argv_flag=NO_TRUST_WORKSPACE_FLAG,
                default_on=False,
                hint=(
                    "By default CSM adds this folder to ~/.codex/config.toml "
                    "as trusted before starting, because codex won't run a "
                    "turn in a folder it hasn't seen — it opens a trust "
                    "prompt the chat view can't show, so the session looks "
                    "alive but silently eats your first message. Check this "
                    "to leave the config untouched and answer the prompt "
                    "yourself in the terminal view."
                ),
            ),
            InfoBlock(
                kind="info",
                text=(
                    "Codex also runs with -C <cwd> -s workspace-write "
                    "(sandbox mode) baked in by CSM. The session id is "
                    "discovered from the rollout file after spawn — there "
                    "is no --session-id flag."
                ),
            ),
        ]

    # How long to wait for a fresh rollout file / SQLite thread row to
    # appear during post_spawn_bind before giving up. codex 0.145.0+ with
    # the `sqlite` feature enabled writes the `threads` row and rollout
    # jsonl LAZILY — as of 2026-08-05 empirically ~2 minutes after
    # process start (or on first user message, whichever is first). The
    # SQLite fast-path usually resolves within a few seconds when it hits
    # at all, but the deadline must accommodate the worst case where the
    # filesystem fallback is the only signal.
    _BIND_TIMEOUT_SEC = 180.0
    _BIND_POLL_SEC = 1.0

    def __init__(self) -> None:
        self._tailer: CodexRolloutTailer | None = None
        # SessionManager captures rollout file state AND state-DB thread ids
        # immediately before spawning Codex. Both are needed because codex
        # can either (a) create/grow a rollout jsonl or (b) insert a new
        # `threads` row in state_*.sqlite — whichever happens first wins
        # the bind. A set of paths alone is insufficient: Codex may reopen
        # an existing rollout for a continued thread, so the binder must
        # detect a file that grew after spawn as well as a newly-created path.
        self._bind_baselines: dict[str, _BindBaseline] = {}

    # ---- lazy tailer construction ----
    def _get_tailer(self) -> CodexRolloutTailer:
        if self._tailer is None:
            self._tailer = CodexRolloutTailer(self.artifact_root())
        return self._tailer

    # ---- environment ----
    def home_dir(self) -> Path:
        return _paths.codex_home()

    def default_home_name(self) -> str:
        return ".codex"

    def ensure_workspace_trusted(self, cwd: str) -> bool:
        """Add `[projects."<cwd>"] trust_level = "trusted"` to codex's config.

        Returns True if an entry was written, False if one already existed.

        Why this is necessary at all: codex refuses to start a turn in a
        directory absent from its `[projects]` table. It opens a folder-trust
        modal instead — and CSM's chat surfaces never render the terminal, so
        the user sees a session that accepts messages and does nothing. The
        first message is silently consumed answering a dialog they can't see.
        Confirmed in isolation on a fresh CODEX_HOME: identical spawn in the
        same new directory, `submit=NO` without the entry and `submit=YES`
        with it. Passing the same value via `-c` does NOT work — the trust
        check reads the on-disk config only (measured, not assumed).

        This is exactly the entry codex itself writes once the user answers
        "trust", and it is narrower than the approval bypass CSM already
        forces on every managed session. The user picked this cwd when
        creating the session; that is the consent signal.

        Best effort: any failure returns False and the session still spawns
        (degrading to today's behaviour) rather than blocking session
        creation on a config write.
        """
        import tomllib

        cfg = self.home_dir() / "config.toml"
        try:
            raw = cfg.read_bytes() if cfg.exists() else b""
            parsed = tomllib.loads(raw.decode("utf-8")) if raw else {}
            if isinstance(parsed.get("projects"), dict) and cwd in parsed["projects"]:
                return False
            # Append rather than re-serialise: round-tripping through a TOML
            # writer would drop the user's comments and section ordering.
            escaped = cwd.replace("\\", "\\\\").replace('"', '\\"')
            suffix = (
                f'\n[projects."{escaped}"]\ntrust_level = "trusted"\n'
            )
            body = raw
            if body and not body.endswith(b"\n"):
                body += b"\n"
            cfg.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_with_hash_guard(cfg, body + suffix.encode("utf-8"))
            log.info("codex: pre-trusted workspace %s in %s", cwd, cfg)
            return True
        except Exception:
            log.exception("codex: failed to pre-trust workspace %s", cwd)
            return False

    def auth_file(self) -> Path | None:
        # Codex stores auth in `<CODEX_HOME>/auth.json`.
        return self.home_dir() / "auth.json"

    def probe(self) -> AdapterStatus:
        binary = shutil.which("codex")
        if binary is None:
            return AdapterStatus(
                name=self.name,
                installed=False,
                authenticated=False,
                error="`codex` binary not on PATH",
                capabilities=self.capabilities,
            )
        version: str | None = None
        try:
            out = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if out.returncode == 0:
                version = (out.stdout or out.stderr).strip().splitlines()[0]
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            return AdapterStatus(
                name=self.name,
                installed=True,
                authenticated=False,
                error=f"`codex --version` failed: {e}",
                capabilities=self.capabilities,
            )
        auth_ok = self.auth_file() is not None and self.auth_file().exists()
        return AdapterStatus(
            name=self.name,
            installed=True,
            authenticated=auth_ok,
            version=version,
            error=None if auth_ok else f"missing {self.auth_file()}",
            capabilities=self.capabilities,
        )

    # ---- session-id lifecycle ----
    def pre_spawn_session_id(self, cwd: str) -> str | None:
        """Codex has no `--session-id` flag — id is discovered post-spawn.

        Returns None so `SessionManager` knows to skip argv injection and
        call `post_spawn_bind()` instead.
        """
        return None

    def post_spawn_bind(
        self,
        session_row_id: str,
        cwd: str,
    ) -> PostSpawnBindResult | None:
        """Discover the codex session id + rollout path after spawn.

        Called by SessionManager after the codex process is up. Races two
        detection paths on the same poll tick:

        1. **SQLite fast-path** (codex 0.145.0+ with `sqlite` feature on,
           which is the default and cannot be disabled). Queries
           `~/.codex/state_*.sqlite`'s `threads` table for a row whose
           `cwd` matches and whose `id` was not in the pre-spawn baseline.
           Typically resolves within 30-60s of process start — much faster
           than waiting for codex to flush its rollout jsonl.

        2. **Filesystem fallback** (works with any codex version). Polls
           the rollout tree for a new or newly-grown jsonl whose first
           record is a `session_meta` with `payload.cwd == cwd`. This is
           the only signal on old codex versions and on codex builds that
           somehow have the state DB disabled.

        Returns None on timeout — caller decides whether to retry, log,
        or leave Session.external_session_id / rollout_path NULL (the
        tailer will still eventually pick up the id on a later tick).

        NOTE: This is a synchronous poller (single-threaded I/O only).
        SessionManager wraps this call in `asyncio.to_thread`.
        """
        root = self.artifact_root()
        deadline = time.monotonic() + self._BIND_TIMEOUT_SEC
        # Prefer the snapshot captured *before* process spawn. Falling back to
        # a fresh snapshot keeps direct callers/backward-compatible adapters
        # safe, but SessionManager always prepares the baseline.
        baseline = self._bind_baselines.pop(session_row_id, None)
        if baseline is None:
            baseline = _capture_bind_baseline(root)

        while time.monotonic() < deadline:
            # 1. SQLite fast-path — usually wins within seconds when codex
            # has the state DB enabled.
            hit = _query_new_thread(
                _codex_home_from_sessions_root(root),
                cwd=cwd,
                baseline_thread_ids=baseline.thread_ids,
            )
            if hit is not None:
                thread_id, rollout_path = hit
                return PostSpawnBindResult(
                    external_session_id=thread_id,
                    artifact_path=rollout_path or None,
                )

            # 2. Filesystem fallback — kicks in for old codex versions or
            # when the state DB row is delayed even longer than the jsonl.
            try:
                candidates: list[tuple[int, Path]] = []
                for path in root.rglob("rollout-*.jsonl"):
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    current = (st.st_size, st.st_mtime_ns)
                    if baseline.files.get(str(path)) != current:
                        candidates.append((st.st_mtime_ns, path))
            except OSError:
                candidates = []
            # If several Codex processes share a cwd, the rollout touched most
            # recently is the best fallback. The common case has one candidate.
            candidates.sort(key=lambda item: item[0], reverse=True)
            for _mtime_ns, path in candidates:
                sid_from_file = _read_first_session_meta(path, expected_cwd=cwd)
                if sid_from_file:
                    return PostSpawnBindResult(
                        external_session_id=sid_from_file,
                        artifact_path=str(path),
                    )
                # Do not mark an unreadable/partial candidate as seen. Its
                # session_meta first line may still be mid-flush; retry it on
                # the next poll instead of timing out permanently.
            time.sleep(self._BIND_POLL_SEC)
        log.warning(
            "codex post_spawn_bind timed out for session %s cwd=%s "
            "after %.1fs — tailer will pick up id lazily on next tick",
            session_row_id,
            cwd,
            self._BIND_TIMEOUT_SEC,
        )
        return None

    def prepare_post_spawn_bind(self, session_row_id: str) -> None:
        """Capture rollout file state AND state-DB thread ids before spawn."""
        self._bind_baselines[session_row_id] = _capture_bind_baseline(self.artifact_root())

    def cancel_post_spawn_bind(self, session_row_id: str) -> None:
        """Discard a prepared baseline when process creation fails."""
        self._bind_baselines.pop(session_row_id, None)

    # ---- argv ----
    def frame_pty_input(self, text: str) -> bytes:
        """Wrap the message as a bracketed paste, then submit.

        Codex's TUI DISCARDS a plain `text + CRLF` burst — verified against
        codex-cli 0.145.0 driven through a real PTY: the turn never starts
        and the text never even appears in the composer. Typing the same
        bytes one at a time works, which is what identifies this as input
        FRAMING rather than a readiness race (25s of warm-up made no
        difference, and an established session behaved identically).

        Codex advertises bracketed-paste support (`CSI ?2004h`) at startup,
        so `ESC[200~ … ESC[201~` is the shape it is asking for — this is what
        a terminal emulator sends when you paste. The trailing `\\r` may ride
        along in the SAME write: verified submitting, which keeps the caller's
        single atomic PTY write (and therefore its idempotency lock) intact.
        """
        return b"\x1b[200~" + text.encode("utf-8", errors="replace") + b"\x1b[201~\r"

    def frame_pty_input_sequence(self, text: str) -> list[bytes]:
        """One write. Unlike claude, codex was VERIFIED to submit with the CR
        riding inside the same burst, so there is nothing to split — and a
        gratuitous split would only add latency to every codex send."""
        return [self.frame_pty_input(text)]

    def build_argv(
        self,
        base_argv: list[str],
        cwd: str,
        *,
        session_id: str | None = None,  # noqa: ARG002  (codex has no pre-spawn id)
        initial_prompt: str | None = None,
        extra_args: list[str] | None = None,
        resume_from: str | None = None,
    ) -> AdapterArgvResult:
        """Delegate to `build_codex_argv` (still in spawners.py during M1).

        Non-codex argv[0] (`["bash", "-i"]`) is strict pass-through — the
        existing spawner already enforces this contract.
        """
        result = build_codex_argv(
            base_argv=base_argv,
            cwd=cwd,
            initial_prompt=initial_prompt,
            extra_args=extra_args,
            resume_from=resume_from,
        )
        return AdapterArgvResult(
            argv=result.argv,
            # A resumed Codex row can bind immediately; a brand-new row still
            # gets its id from post_spawn_bind.
            session_id=resume_from,
            prompt_appended=result.prompt_appended,
        )

    # ---- event streaming ----
    def artifact_root(self) -> Path:
        return _paths.codex_sessions_dir()

    def artifact_glob(self) -> str:
        return str(self.artifact_root() / "**" / "rollout-*.jsonl")

    def scan_events(self) -> list[Event]:
        """Tail rollout jsonls + derive canonical events.

        Codex emits SESSION_STARTED itself from the `session_meta` record
        (see derive_codex_events); no separate newly-seen bookkeeping
        needed like the claude side.
        """
        tailer = self._get_tailer()
        records = tailer.scan_once()
        events: list[Event] = []
        for r in records:
            events.extend(derive_codex_events(r))
        return events

    def snapshot(self) -> dict[str, Any]:
        return self._get_tailer().snapshot()

    def restore(self, snap: dict[str, Any]) -> None:
        self._get_tailer().restore(snap)

    def tail_states(self) -> list[dict[str, Any]]:
        """Watchdog view. Codex's session id is bootstrapped from
        session_meta on line 0; may be empty until that line is parsed."""
        out: list[dict[str, Any]] = []
        for path, state in self._get_tailer().file_states().items():
            out.append(
                {
                    "path": path,
                    "external_session_id": state.codex_session_id or None,
                    "project_path": state.project_path or None,
                    "mtime": state.mtime,
                }
            )
        return out

    def take_newly_seen(self) -> set[str]:
        """Codex's SESSION_STARTED is derived from a record (session_meta),
        not from newly-seen file detection — so this always returns empty.
        Provided for Protocol conformance."""
        return set()

    def install_hooks(self, project_root: Path, callback_url: str) -> None:
        """Codex hooks are not wired yet — P5 is blocked on live event
        payload capture (see codex_hooks.py header). This is a no-op that
        satisfies the Protocol; capability HOOKS is NOT in
        `self.capabilities`, so SessionManager should never call this.
        """
        pass

    # ---- multi-agent config sync (P0 v3 · Phase 2.5) -----------------
    # Symmetric with ClaudeAdapter for memory + mcp + skills. Skills were
    # historically off ("codex has no skills dir as of 2026-07") but codex-cli
    # 0.145.0 ships `~/.codex/skills/<name>/SKILL.md` with the same layout as
    # claude, so skills_dir()/list/write/remove are now implemented and
    # SYNC_SKILLS is granted by the capability probe.

    def memory_paths(self, scope: str) -> list[Path]:
        """Codex memory files.

        - user  : `~/.codex/AGENTS.md`.
        - project: [] (per-cwd AGENTS.md handled by SyncService directly).
        """
        if scope == "user":
            return [self.home_dir() / "AGENTS.md"]
        return []

    def read_memory(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def read_memory_full(self, scope: str) -> str | None:
        paths = self.memory_paths(scope)
        if not paths:
            return None
        return "\n\n".join(self.read_memory(p) for p in paths)

    def write_memory_marker_block(
        self,
        path: Path,
        marker_id: str,
        body: str,
    ) -> None:
        """Insert / replace the `csm:start id=<marker_id>` block in `path`.

        Uses B1 atomic_write_with_hash_guard. Symmetric with the claude
        implementation — codex does not manage AGENTS.md through its own
        CLI, so we just do a direct file write.
        """
        current = self.read_memory(path)
        updated = replace_or_append_marker_block(
            current,
            self.marker_syntax(),
            marker_id,
            body,
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
        """Add MCP entry via `codex mcp add`. Idempotent via mcp_list()."""
        existing = await self.mcp_list()
        if any(e.get("name") == name for e in existing):
            return CLIResult(
                argv=("codex", "mcp", "add", name, "<skipped: already exists>"),
                returncode=0,
                stdout="",
                stderr="",
                duration_ms=0,
                timed_out=False,
            )

        argv: list[str] = ["codex", "mcp", "add", name, "--transport", transport]
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
        """Remove `name` via `codex mcp remove`. Idempotent when absent."""
        existing = await self.mcp_list()
        if not any(e.get("name") == name for e in existing):
            return CLIResult(
                argv=("codex", "mcp", "remove", name, "<already absent>"),
                returncode=0,
                stdout="",
                stderr="not found",
                duration_ms=0,
                timed_out=False,
            )
        return await run_cli(["codex", "mcp", "remove", name], timeout=10.0)

    async def mcp_list(self) -> list[dict[str, Any]]:
        """Parse `codex mcp list` (same shape as claude — line-based)."""
        r = await run_cli(["codex", "mcp", "list"], timeout=5.0)
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
        """`threads.title` for `external_id`, read from codex's own sqlite.

        Blocking IO — the caller runs it off the event loop (see the
        EXTERNAL_TITLE contract in backends/base.py).
        """
        return query_codex_thread_title(self.home_dir(), external_id)

    def skills_dir(self) -> Path | None:
        """Codex skills live at `~/.codex/skills/<name>/SKILL.md`.

        Confirmed present on codex-cli 0.145.0 — the on-disk format matches
        claude's skills tree exactly. codex ships built-in system skills
        under the `.system/` subdir; those are excluded from list/import
        (see list_skills) so they never get adopted into CSM as user skills.
        """
        return self.home_dir() / "skills"

    def list_skills(self) -> list[dict[str, Any]]:
        """List `<name>/SKILL.md` under skills_dir(), skipping dot-dirs.

        `.system/` (codex's built-in skills) and any other dot-prefixed
        directory are excluded — only user skills are surfaced for sync.
        Symmetric with ClaudeAdapter.list_skills() plus the dot-dir filter
        (claude's skills tree has no analogous system subdir).
        """
        return _skill_fs.list_skills(self.skills_dir(), skip_dot_dirs=True)

    def list_skills_full(self) -> list[dict[str, Any]]:
        """`list_skills()` plus body_md, file_count and bundle_hash."""
        return _skill_fs.list_skills_full(self.skills_dir(), skip_dot_dirs=True)

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
        """Runtime probe of the SYNC_* capability set.

        - SYNC_MEMORY: always (markdown file, no CLI subcommand).
        - SYNC_MCP: probed via `codex mcp --help`.
        - SYNC_SKILLS: codex-cli has NO `skill` subcommand, so we can't probe
          it the way claude does (`claude skill --help`). codex skills are a
          pure directory convention (`~/.codex/skills/<name>/SKILL.md`,
          confirmed on codex-cli 0.145.0, format identical to claude). We
          therefore gate the capability on `skills_dir()` being available
          rather than on a CLI probe.
        """
        caps: set[Capability] = {Capability.SYNC_MEMORY}
        probed = await _probe_helper("codex")
        if "mcp" in probed:
            caps.add(Capability.SYNC_MCP)
        if self.skills_dir() is not None:
            caps.add(Capability.SYNC_SKILLS)
        return frozenset(caps)


def _read_first_session_meta(
    path: Path,
    *,
    expected_cwd: str | None = None,
) -> str | None:
    """Read the first JSONL line of `path`. If it's a `session_meta` and
    (optionally) its cwd matches `expected_cwd`, return its `session_id`.
    Returns None otherwise (file not ready yet, wrong session, etc.).
    """
    try:
        with open(path, "rb") as f:
            first = f.readline()
    except OSError:
        return None
    if not first.endswith(b"\n"):
        # Writer mid-flush — try again next poll.
        return None
    try:
        import json as _json

        obj = _json.loads(first)
    except (ValueError, TypeError):
        return None
    if obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload") or {}
    if not isinstance(payload, dict):
        return None
    if expected_cwd is not None and str(payload.get("cwd") or "") != expected_cwd:
        return None
    sid = payload.get("session_id") or payload.get("id")
    return str(sid) if sid else None


def _snapshot_rollouts(root: Path) -> dict[str, tuple[int, int]]:
    """Return path -> (size, mtime_ns) for rollout files under ``root``.

    Best-effort by design: files can rotate between glob and stat. Missing
    entries simply look new to the post-spawn binder on its next poll.
    """
    snapshot: dict[str, tuple[int, int]] = {}
    try:
        paths = root.rglob("rollout-*.jsonl")
        for path in paths:
            try:
                st = path.stat()
            except OSError:
                continue
            snapshot[str(path)] = (st.st_size, st.st_mtime_ns)
    except OSError:
        pass
    return snapshot


@dataclass
class _BindBaseline:
    """Captured pre-spawn state used to detect which artifact the freshly
    spawned codex process wrote (or grew).

    - `files`: rollout jsonl path -> (size, mtime_ns). A missing entry OR
      a differing (size, mtime_ns) marks the file as post-spawn activity.
    - `thread_ids`: set of thread ids present in the codex state DB. Any
      row not in this set is treated as new. Empty set on codex builds
      without a state DB — the SQLite fast-path just never fires.
    """

    files: dict[str, tuple[int, int]] = field(default_factory=dict)
    thread_ids: frozenset[str] = frozenset()


def _capture_bind_baseline(sessions_root: Path) -> _BindBaseline:
    """Snapshot both rollout files and state-DB thread ids before spawn.

    Callable from anywhere; failures in either source degrade gracefully
    to an empty snapshot (which just means "everything looks new" and the
    binder may briefly attribute a truly unrelated thread — acceptable
    given SessionManager always calls this exactly once immediately before
    fork, so the race window is sub-second).
    """
    home = _codex_home_from_sessions_root(sessions_root)
    return _BindBaseline(
        files=_snapshot_rollouts(sessions_root),
        thread_ids=_snapshot_thread_ids(home),
    )


def _codex_home_from_sessions_root(sessions_root: Path) -> Path:
    """The parent of `~/.codex/sessions/` is `~/.codex/`.

    Kept as a helper so the derivation is stated once — tests can inject
    a custom `CSM_CODEX_HOME` and both the sessions dir and the state DB
    resolve consistently.
    """
    return sessions_root.parent


_STATE_DB_RE = re.compile(r"^state_(\d+)\.sqlite$")


def _codex_state_db_paths(home: Path) -> list[Path]:
    """Return codex state DB files under `home`, newest schema version first.

    codex stores per-user thread state at `<CODEX_HOME>/state_<N>.sqlite`.
    N bumps on breaking schema changes; older versions get left in place
    for downgrade compatibility. We probe from the highest N to the lowest
    so a newer codex install wins. As of codex 0.145.0 the current file is
    `state_5.sqlite` — the regex + sort keeps working when N advances.
    """
    candidates: list[tuple[int, Path]] = []
    try:
        for path in home.glob("state_*.sqlite"):
            m = _STATE_DB_RE.match(path.name)
            if m and path.is_file():
                candidates.append((int(m.group(1)), path))
    except OSError:
        return []
    candidates.sort(key=lambda item: item[0], reverse=True)
    return [p for _n, p in candidates]


def query_codex_thread_title(home: Path, thread_id: str) -> str | None:
    """Read `threads.title` for a given codex thread id.

    codex 0.145+ stores per-thread state in `state_<N>.sqlite`; the
    `title` column is auto-populated from the first user prompt on
    thread creation and is otherwise not user-editable in that CLI's
    current shape. `local:7a422f9d` uses this to reflect the codex-
    side title back into `session.title` when the CSM user hasn't
    claimed the field via `title_manual`.

    Returns None if:
      - no state DB exists (older codex builds / clean test env),
      - the thread id is not present (row hasn't been flushed yet),
      - the title column is empty / null.
    Silently swallows sqlite errors so a schema drift on a stale DB
    file can't take down the caller.
    """
    if not thread_id:
        return None
    dbs = _codex_state_db_paths(home)
    for db_path in dbs:
        conn = _open_state_db_ro(db_path)
        if conn is None:
            continue
        try:
            cur = conn.execute(
                "SELECT title FROM threads WHERE id = ? LIMIT 1",
                (thread_id,),
            )
            row = cur.fetchone()
            if row and row[0]:
                title = str(row[0]).strip()
                return title or None
        except sqlite3.Error:
            continue
        finally:
            conn.close()
    return None


def _snapshot_thread_ids(home: Path) -> frozenset[str]:
    """Return every thread id currently in the newest codex state DB.

    Empty frozenset when there is no state DB (older codex builds, or a
    test env without one). Used as the baseline exclusion set so
    `_query_new_thread` only returns rows codex inserted AFTER spawn.
    """
    dbs = _codex_state_db_paths(home)
    if not dbs:
        return frozenset()
    ids: set[str] = set()
    for db_path in dbs:
        conn = _open_state_db_ro(db_path)
        if conn is None:
            continue
        try:
            cur = conn.execute("SELECT id FROM threads")
            ids.update(str(row[0]) for row in cur.fetchall() if row[0])
        except sqlite3.Error:
            # `threads` may not exist on very old schemas; treat as empty.
            pass
        finally:
            conn.close()
    return frozenset(ids)


def _query_new_thread(
    home: Path,
    *,
    cwd: str,
    baseline_thread_ids: frozenset[str],
) -> tuple[str, str | None] | None:
    """Look for a `threads` row in `cwd` whose id isn't in `baseline_thread_ids`.

    Returns `(thread_id, rollout_path)` on match, or `None` when nothing
    new has appeared yet. `rollout_path` may be `None` when the codex row
    was inserted before the file was flushed; caller keeps the id and lets
    the tailer fill the path later.

    Tie-breaking when multiple rows are "new": the row with the SMALLEST
    ``created_at`` wins. SessionManager calls `prepare_post_spawn_bind`
    then IMMEDIATELY forks codex, so our thread has the earliest post-
    baseline `created_at`. Any competing thread inserted later (another
    codex process the user started manually, an autonomous background
    job, etc.) will have a strictly larger `created_at` and be correctly
    ignored — even though it sorts first when listed newest-first.
    """
    dbs = _codex_state_db_paths(home)
    for db_path in dbs:
        conn = _open_state_db_ro(db_path)
        if conn is None:
            continue
        try:
            cur = conn.execute(
                "SELECT id, rollout_path, created_at FROM threads "
                "WHERE cwd = ? "
                "ORDER BY created_at DESC, id DESC LIMIT 50",
                (cwd,),
            )
            new_rows: list[tuple[int, str, str | None]] = []
            for tid, rpath, created in cur.fetchall():
                if not tid:
                    continue
                tid_s = str(tid)
                if tid_s in baseline_thread_ids:
                    continue
                new_rows.append(
                    (int(created or 0), tid_s, str(rpath) if rpath else None),
                )
            if not new_rows:
                continue
            # Earliest post-baseline created_at = our spawn.
            new_rows.sort(key=lambda item: item[0])
            _created, tid, rpath = new_rows[0]
            return (tid, rpath)
        except sqlite3.Error:
            # Schema mismatch on this DB file; fall through to the next.
            continue
        finally:
            conn.close()
    return None


def _open_state_db_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open a codex state DB in read-only mode with a short lock timeout.

    codex holds the DB for writes but supports concurrent readers; SQLite
    WAL mode lets us read without blocking. `timeout=1.0` caps the wait
    when a competing writer is mid-checkpoint — we'd rather fall back to
    filesystem polling than stall the bind loop.
    """
    try:
        # `mode=ro` also implies no journal creation attempts. Uri form
        # is required to pass `mode`.
        return sqlite3.connect(
            f"file:{db_path}?mode=ro",
            uri=True,
            timeout=1.0,
        )
    except sqlite3.Error:
        return None


__all__ = ["CodexAdapter"]
