"""CLI-adapter contract.

An `CLIAdapter` encapsulates everything backend-specific about a single CLI
(claude, codex, future gemini, ...): argv construction, session-id lifecycle,
artifact tailing, event derivation, environment paths, and optional hooks.

Domain services (SessionManager, EventStream, Workflow, Supervisor,
AgentDeck) MUST NOT special-case on adapter name — every `if agent ==
'claude'` outside `csm/backends/` is a leak. Look up the adapter by name
through `AdapterRegistry` and delegate.

# Design notes

## Two session-id hooks, not one

Claude and Codex have fundamentally different session-id lifecycles:

- **Claude** generates the id BEFORE spawn and injects it as `--session-id
  <uuid>`. Sync, always-succeeds.
- **Codex** has no such flag. The CLI writes a rollout file and the id
  lives inside the file's first `session_meta` record. CSM must discover
  it AFTER spawn by tailing the rollout dir. Async, may fail.

Squashing these into one method (`detect_session_id(spawn_ctx, ...)`)
leaves both implementations unclear when to run and what None means. So
the contract has two orthogonal hooks:

    pre_spawn_session_id(cwd) -> str | None    # claude: uuid, codex: None
    post_spawn_bind(session_row_id, cwd) -> str | None  # codex reads rollout, claude: None

Adapters declare which they support via `Capability.PRE_SPAWN_SESSION_ID`
/ `Capability.POST_SPAWN_BIND` — SessionManager reads capabilities to
decide the spawn dance.

## Adapter-owned tail state

Each adapter owns its own tailer + memoisation. `scan_events()` is the
single entry point EventStream calls — it internally does incremental
tail + parse + derive. `snapshot()` / `restore()` let EventStream persist
per-adapter offset state across restarts (into `file_state` table with
`agent` column).

`take_newly_seen()` returns file paths first observed in the latest
scan; adapter contract is that a SESSION_STARTED event is either yielded
alongside (Codex — from `session_meta` record) or inferred by
EventStream from newly-seen tuples (Claude — first line may be any
message). Adapters are free to pick either strategy internally; the
event stream loop just iterates `scan_events()` output.

## Canonical events

Every adapter's `scan_events()` yields `csm.core.events.Event` instances.
CLI-specific signals (codex reasoning tokens, claude cache hit rate)
land in `Event.payload` under the `_<agent>_*` prefix. Downstream code
MAY read these fields but MUST NOT branch on them for control flow (that
would re-introduce the discriminated pattern this layer exists to kill).
See `docs/backends/canonical_events.md`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from csm.core.events import Event

if TYPE_CHECKING:
    # Referenced by CLIAdapter.mcp_add / mcp_remove signatures. Kept
    # under TYPE_CHECKING because sync depends on backends (adapter
    # capability probes call sync.cli_runner.run_cli); pulling the
    # module in eagerly would flip the import direction at boot.
    from csm.modules.sync.cli_runner import CLIResult


class Capability(StrEnum):
    """Optional adapter feature flags.

    Adapters declare their capability set at construction time. Domain code
    reads `capabilities` to decide whether to call optional methods (hooks
    installation, pre-spawn id injection) instead of relying on backend
    identity.
    """
    # Adapter provides a pre-spawn --session-id style flag (claude).
    PRE_SPAWN_SESSION_ID = "pre_spawn_session_id"
    # Adapter discovers session id post-spawn from a written artifact (codex).
    POST_SPAWN_BIND = "post_spawn_bind"
    # Adapter can install per-project hook config that POSTs back to CSM
    # (claude only, as of 2026-07-25).
    HOOKS = "hooks"
    # Adapter's CLI supports interactive streaming stdio (both claude & codex).
    INTERACTIVE_STREAM = "interactive_stream"
    # Adapter can reopen an existing external conversation id in a fresh PTY.
    RESUME_SESSION = "resume_session"
    # Adapter can resolve a human-readable title for an external session id
    # out of the CLI's own state store (codex: `threads.title` in
    # `state_<N>.sqlite`). Claude has no equivalent — it publishes titles as
    # `custom-title` / `ai-title` records inside the JSONL transcript, which
    # the tailer already picks up, so it does not declare this.
    EXTERNAL_TITLE = "external_title"

    # ---- Multi-agent config sync (P0 v3) ------------------------------
    # Adapter can read/write the CLI's memory files (CLAUDE.md / AGENTS.md)
    # via marker-fenced blocks. See docs/backends/multi_agent_sync_spec.md §8.
    SYNC_MEMORY = "sync_memory"
    # Adapter's CLI supports `mcp add / remove / list` subcommands. Probed
    # at construction; absent → sync layer skips this adapter for MCP.
    SYNC_MCP = "sync_mcp"
    # Adapter's CLI scans a skills directory under home_dir(). For claude:
    # ~/.claude/skills/. For codex: pending upstream support.
    SYNC_SKILLS = "sync_skills"


@dataclass(frozen=True)
class MarkerSyntax:
    """Fencing tokens for a memory marker block.

    Rendered as (using default HTML-comment style):
        <open_prefix> csm:start id={marker_id} <open_suffix>
        <body content>
        <close_prefix> csm:end id={marker_id} <close_suffix>

    The block is idempotently replaceable by matching the outer fences
    with `marker_id` — even if the body has been externally edited,
    the drift poll will detect divergence via hash compare.
    """
    open_prefix: str
    open_suffix: str
    close_prefix: str
    close_suffix: str

    @classmethod
    def html_comment(cls) -> MarkerSyntax:
        """Default fence style — both claude and codex use markdown files."""
        return cls("<!--", "-->", "<!--", "-->")


@dataclass(frozen=True)
class AdapterStatus:
    """Result of `adapter.probe()` — used by `/api/backends` for UI.

    `installed` means the CLI binary was found. `authenticated` means auth
    config passes a lightweight check (file exists / API returns 200 for a
    cheap request). `error` is a human-readable one-liner for the UI to
    show when the adapter is unusable.
    """
    name: str
    installed: bool
    authenticated: bool
    version: str | None = None
    error: str | None = None
    capabilities: frozenset[Capability] = field(default_factory=frozenset)

    @property
    def usable(self) -> bool:
        """True iff a spawn is expected to succeed."""
        return self.installed and self.authenticated and self.error is None


# ---------------------------------------------------------------------------
# Flag schema — declarative UI for the frontend's "New session" dialog.
# ---------------------------------------------------------------------------
#
# Each adapter returns a `flags_schema()` list of these descriptors describing
# per-adapter argv customisation options. The frontend `<AdapterFlagsPanel>`
# component renders them generically — checkbox / select / resume-picker / info
# blob — WITHOUT branching on adapter name. Adding a new adapter is a matter of
# declaring its schema; no frontend changes required.
#
# The `argv_flag` field is the CLI argument name (e.g. "--model"). The panel
# uses it to mutate the current argv string:
#   - checkbox: presence/absence toggles the flag
#   - select:   sets `<argv_flag> <value>` when value is truthy, removes when empty
#   - resume:   same as select, but choices come from the sessions store
#               (the panel looks up currently-resumable sessions for this
#               adapter and populates the dropdown live)
#
# `info` has no argv effect — it just renders a hint blob (e.g. "Codex runs
# with -C ... by default"). Adapters use it to explain non-configurable
# behaviour to the user.


@dataclass(frozen=True)
class SelectChoice:
    value: str          # the actual argv value (e.g. "sonnet")
    label: str          # human display (e.g. "Sonnet 4.5 (default)")


@dataclass(frozen=True)
class CheckboxFlag:
    kind: Literal["checkbox"]
    name: str           # internal id
    label: str          # UI label
    argv_flag: str      # e.g. "--dangerously-skip-permissions"
    hint: str | None = None
    default_on: bool = False


@dataclass(frozen=True)
class SelectFlag:
    kind: Literal["select"]
    name: str
    label: str
    argv_flag: str      # e.g. "--model"
    choices: tuple[SelectChoice, ...]
    hint: str | None = None


@dataclass(frozen=True)
class ResumeFlag:
    """Special kind — panel populates choices dynamically from the sessions
    store, filtering to sessions of this adapter that can be resumed."""
    kind: Literal["resume"]
    name: str
    label: str
    argv_flag: str      # e.g. "--resume"
    hint: str | None = None


@dataclass(frozen=True)
class InfoBlock:
    """Static explanatory text; no interaction. Adapters use this for
    'by default we run with these flags baked in — nothing to configure'
    kind of messaging."""
    kind: Literal["info"]
    text: str


# Discriminated union — Pydantic-friendly, JSON-serialisable via `kind`.
FlagDescriptor = CheckboxFlag | SelectFlag | ResumeFlag | InfoBlock


def flag_to_dict(f: FlagDescriptor) -> dict[str, Any]:
    """Serialise a FlagDescriptor to a plain dict for API responses.

    The `kind` field is the discriminator — frontend switches on it.
    """
    if isinstance(f, InfoBlock):
        return {"kind": "info", "text": f.text}
    if isinstance(f, CheckboxFlag):
        return {
            "kind": "checkbox", "name": f.name, "label": f.label,
            "argv_flag": f.argv_flag, "hint": f.hint,
            "default_on": f.default_on,
        }
    if isinstance(f, SelectFlag):
        return {
            "kind": "select", "name": f.name, "label": f.label,
            "argv_flag": f.argv_flag,
            "choices": [{"value": c.value, "label": c.label} for c in f.choices],
            "hint": f.hint,
        }
    if isinstance(f, ResumeFlag):
        return {
            "kind": "resume", "name": f.name, "label": f.label,
            "argv_flag": f.argv_flag, "hint": f.hint,
        }
    raise TypeError(f"unknown FlagDescriptor: {type(f).__name__}")


@dataclass
class AdapterArgvResult:
    """Return of `adapter.build_argv()`.

    `argv` is the exact list to pass to `Popen` / `pty.fork`. `session_id`
    is the session id embedded in argv (claude's `--session-id <uuid>` case)
    or None (codex — id comes later via `post_spawn_bind`). `prompt_appended`
    signals whether initial_prompt made it into argv — SessionManager uses
    this to decide whether to also fall back to PTY-write of the prompt.
    """
    argv: list[str]
    session_id: str | None = None
    prompt_appended: bool = False


@dataclass
class PostSpawnBindResult:
    """Return of `adapter.post_spawn_bind()`.

    `external_session_id` — the CLI-assigned session id discovered after
    the subprocess started (codex: `session_meta.session_id` from the
    rollout file). Written to `Session.external_session_id` — the field
    that every downstream module (Notification, Runner, Orchestrator,
    Aggregator) uses to correlate events back to a CSM row.

    `artifact_path` — absolute path to the CLI's per-session artifact
    file, when the adapter has one (codex: the rollout .jsonl path;
    claude doesn't need this because the path is derivable from
    `~/.claude/projects/<cwd>/<uuid>.jsonl`). Written to
    `Session.rollout_path`.

    Both fields may be None (e.g. bind timed out — Session.external_session_id
    stays NULL and the tailer will lazily backfill it later). Both must
    NOT be empty strings — treat empty as None on read.
    """
    external_session_id: str | None = None
    artifact_path: str | None = None


@runtime_checkable
class CLIAdapter(Protocol):
    """The contract every backend adapter implements.

    All methods are synchronous except where a comment says otherwise.
    Adapters are constructed once at boot and registered with the
    `AdapterRegistry`; they hold their own state (tail memoisation,
    probe cache) as instance attributes.
    """

    # ---- identity ----
    name: str                          # short id: "claude" | "codex" | "gemini"
    display_name: str                  # UI label: "Claude Code" | "Codex CLI"
    icon: str                          # UI icon glyph (single char or emoji)
    color: str                         # UI accent — hex or `var(--...)`. Renders
                                       # AgentBadge background; frontend has no
                                       # per-adapter CSS.
    capabilities: frozenset[Capability]

    # ---- UI schema (drives frontend New Session dialog + AgentBadge) ----
    def default_argv(self) -> str:
        """Baseline argv string the New Session dialog defaults into when
        this adapter is selected. Example: "claude --dangerously-skip-permissions"
        for ClaudeAdapter; "codex" for CodexAdapter. Frontend uses this to
        pre-populate the Command input; user can still edit it."""

    def flags_schema(self) -> list[FlagDescriptor]:
        """Declarative list of per-adapter UI flags the New Session dialog
        should render. See FlagDescriptor. Return `[]` if the adapter has
        no configurable flags (frontend will hide the panel)."""

    # ---- environment ----
    def home_dir(self) -> Path:
        """Absolute path to the CLI's config/artifacts root.
        Claude: ~/.claude ; Codex: ~/.codex . Honors sandbox env overrides."""

    def default_home_name(self) -> str:
        """The BASENAME of the real user home dir (used by sandbox guard
        to check `home_dir() != Path.home() / default_home_name()`).
        Claude: '.claude' ; Codex: '.codex'."""

    def auth_file(self) -> Path | None:
        """Absolute path to the CLI's auth credentials file, or None if
        the CLI has no such concept."""

    def probe(self) -> AdapterStatus:
        """Cheap synchronous status check for UI. Never raises; returns
        a status with `installed=False` on any error."""

    # ---- spawn: session-id lifecycle (2 hooks per architecture design) ----
    def pre_spawn_session_id(self, cwd: str) -> str | None:
        """Return a session id to bake into argv (via --session-id).

        Claude: fresh uuid4. Codex: None (no such flag). Called by
        SessionManager IFF `PRE_SPAWN_SESSION_ID in capabilities`.
        """

    def post_spawn_bind(
        self, session_row_id: str, cwd: str,
    ) -> PostSpawnBindResult | None:
        """Discover the CLI's own session id + artifact path after spawn.

        Codex: block until the tailer sees session_meta in a new rollout
        file, then return `PostSpawnBindResult(external_session_id=...,
        artifact_path=<rollout file>)`. Claude: returns None (id was
        already injected pre-spawn).

        Called async by SessionManager IFF `POST_SPAWN_BIND in
        capabilities`. Adapter implementations decide how long to block;
        SessionManager wraps this in a timeout.
        """

    # ---- spawn: argv construction ----
    def build_argv(
        self,
        base_argv: list[str],
        cwd: str,
        *,
        session_id: str | None = None,
        initial_prompt: str | None = None,
        extra_args: list[str] | None = None,
        resume_from: str | None = None,
    ) -> AdapterArgvResult:
        """Build the process argv.

        `base_argv` is the caller's baseline (typically `["claude"]` or
        `["codex"]`; tests pass `["bash", "-i"]` etc.). Non-native argv[0]
        MUST be strict pass-through with no flag injection or prompt
        appending — appending arbitrary strings to bash argv leaks them to
        `/proc/<pid>/cmdline` and can turn a prompt into a script path.

        `session_id` is what `pre_spawn_session_id()` returned (may be
        None). Adapter is responsible for embedding it (or not) in argv.
        `resume_from` is an existing adapter-owned conversation id; adapters
        declaring RESUME_SESSION must translate it to their native CLI form.
        """

    # ---- interactive input ----
    def frame_pty_input(self, text: str) -> bytes:
        """Encode one chat message as the bytes to write to the PTY.

        Exists because CLIs do NOT agree on what a "typed line" looks like.
        Claude accepts the obvious `text + CRLF` in a single write. Codex's
        TUI silently DISCARDS a burst that carries the text and the Enter in
        the same read — measured: not submitted, and the text never even
        reaches the composer — so it needs its input wrapped as a paste.

        Callers must use this instead of hand-rolling `text + "\\r\\n"`; that
        assumption is what made every mobile chat message to a codex session
        vanish. Only for prose. Raw key sequences (picker navigation) and
        bare control bytes (Ctrl-C) bypass framing entirely — wrapping those
        would corrupt them.
        """

    def frame_pty_input_sequence(self, text: str) -> list[bytes]:
        """Same message, as the ORDERED PTY writes it needs — one entry per
        write, delivered with a small gap so the CLI reads them separately.

        A single write is not always enough. A TUI decides "typed vs pasted"
        from the shape of one read: claude treats a large burst as a PASTE and
        absorbs a trailing CR into the composer as a literal newline instead of
        acting on it as Enter, so long messages piled up unsent until some
        later short message submitted the whole buffer at once. Pasting and
        then pressing Enter are two separate reads at a real terminal, and this
        is how an adapter says so.

        `[self.frame_pty_input(text)]` is the right answer for any CLI that
        doesn't care, and stays the single-write view for callers that just
        want the bytes. It is spelled out per-adapter rather than defaulted
        here because `CLIAdapter` is a runtime_checkable Protocol that nobody
        inherits from — a body here would never run.
        """

    # ---- event streaming ----
    def artifact_root(self) -> Path:
        """Root dir under which per-session artifact files live.
        Claude: `<home_dir>/projects`. Codex: `<home_dir>/sessions`.
        Used by sandbox guard + seed-idle."""

    def artifact_glob(self) -> str:
        """Absolute glob pattern for artifact files under `artifact_root()`.
        Claude: `<root>/**/*.jsonl`. Codex: `<root>/**/rollout-*.jsonl`.
        Consumed by `_seed_idle_emitted_from_history` in EventStream."""

    def scan_events(self) -> list[Event]:
        """Tail all artifact files incrementally and derive canonical events.

        Returned events include SESSION_STARTED for newly-seen artifacts
        (adapter's own convention — from record for codex, from first-seen
        file for claude). Adapter owns all incremental state and truncation
        detection; EventStream just calls this on each tick.
        """

    def snapshot(self) -> dict[str, Any]:
        """Return per-file tail state (offset, mtime, line_no, adapter-specific).
        Serialisable so EventStream can persist to `file_state` table."""

    def restore(self, snap: dict[str, Any]) -> None:
        """Reload tail state from a prior `snapshot()`."""

    def tail_states(self) -> list[dict[str, Any]]:
        """Per-artifact state the EventStream watchdog needs to derive
        `SESSION_IDLE`.

        Each dict has:
            - `path`: str (absolute artifact path — logging only)
            - `external_session_id`: str | None (CLI-assigned id, matches
              what `post_spawn_bind`/`pre_spawn_session_id` returns).
              `None` means "artifact seen but no id yet extracted".
            - `project_path`: str | None (adapter-derived cwd)
            - `mtime`: float (last observed mtime)

        Watchdog emits `SESSION_IDLE` keyed by `external_session_id`, so
        downstream lookups (Notification, Runner, Orchestrator) resolve
        correctly to their Session row via `Session.external_session_id`.
        """

    def take_newly_seen(self) -> set[str]:
        """Return + clear paths first observed in the most recent `scan_events()`.
        EventStream uses this for observability + one-shot bookkeeping."""

    # ---- hooks (capability-gated; only called if HOOKS in capabilities) ----
    def install_hooks(self, project_root: Path, callback_url: str) -> None:
        """Write per-project hook config that POSTs events back to CSM.

        Reserved, and currently called from nowhere — don't read a claude
        session's working hooks as evidence that this ran. Claude declares
        `Capability.HOOKS` but injects its hook config through `--settings`
        at spawn time (see its `build_argv`), so its implementation here is a
        documented no-op. The method stays for an adapter whose CLI can only
        be hooked by writing files into the project; if you implement it,
        wire the call site too, because there isn't one yet.
        """

    # ---- multi-agent config sync (P0 v3) -----------------------------
    # All 12 methods are guarded by the corresponding SYNC_* Capability
    # in `capabilities`. SyncService NEVER special-cases on adapter name;
    # if the cap is missing it skips (see docs/backends/multi_agent_sync_spec.md §8).

    # -- memory sync --
    def memory_paths(self, scope: Literal["user", "project"]) -> list[Path]:
        """Ordered list of files CSM may write marker blocks into.

        - scope="user"    : global config, e.g. ~/.claude/CLAUDE.md
                                          or ~/.codex/AGENTS.md.
        - scope="project" : per-repo config (if the CLI supports it),
                            e.g. <project_root>/CLAUDE.md — else [].

        List order defines write priority; SyncService writes to the
        first path that exists OR (if none exist) creates the first.
        Empty list means the adapter does not expose memory for that scope.
        """

    def read_memory(self, path: Path) -> str:
        """Read a memory file; return "" if it does not exist.

        Never raises for missing files (empty-state is expected). Raises
        OSError only for genuine I/O failures.
        """

    def read_memory_full(self, scope: Literal["user", "project"]) -> str | None:
        """Return the FULL memory-file contents for `scope`, concatenated
        across `memory_paths(scope)` in listed order (joined by two
        newlines).

        Used by SyncAgent's input-payload builder — the agent needs the
        entire memory text (not just per-marker bodies) to reason about
        adopt / propagate proposals. Callers must NOT parse marker blocks
        from this string; use `read_memory(path)` + marker extraction
        helpers for that.

        Returns None when `memory_paths(scope)` is empty AND the adapter
        has no memory concept for this scope. Returns "" when the paths
        exist but every file is empty / missing (empty-state, not
        error-state).
        """

    def write_memory_marker_block(
        self, path: Path, marker_id: str, body: str,
    ) -> None:
        """Write / replace a single marker-fenced block inside `path`.

        Marker fencing is per-adapter (see `marker_syntax()`). Existing
        blocks with `marker_id` are REPLACED in place; otherwise the
        block is APPENDED at end of file.

        MUST use `atomic_write_with_hash_guard()` internally so
        concurrent REPL writes are surfaced as ConcurrentWriteDrift.
        Never retries — SyncService catches and records the drift.
        """

    # -- mcp sync --
    async def mcp_add(
        self,
        name: str,
        *,
        transport: Literal["stdio", "http", "sse"],
        command: str | None = None,
        args: list[str] | None = None,
        url: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CLIResult:
        """Invoke `<cli> mcp add <name> [...]`; return the raw CLIResult.

        Idempotent contract: identical args twice MUST both return
        returncode==0. Adapters lacking native idempotence should
        first call `mcp_list()` and short-circuit.

        `env` values are passed to the child's environment dict (per B5) —
        NOT concatenated into argv. Caller runs `resolve_env_refs()`
        before this call.

        Called only when SYNC_MCP in capabilities.
        """

    async def mcp_remove(self, name: str) -> CLIResult:
        """Invoke `<cli> mcp remove <name>`.

        Idempotent: removing a non-existent name returns returncode==0.
        """

    async def mcp_list(self) -> list[dict[str, Any]]:
        """Return currently-installed MCP entries.

        Shape: `[{"name": str, "transport": str, "command"|"url": str, ...}]`.
        Drift poll uses this to detect external edits.
        """

    async def list_mcp_servers_full(self) -> list[dict[str, Any]]:
        """SyncAgent-facing enumeration of MCP servers.

        Same shape as `mcp_list()` but with any adapter-specific enrichment
        (e.g. command / url / env if the CLI exposes them). Semantically
        identical to `mcp_list()` for adapters whose CLIs only surface
        name+transport at listing time.

        The sentinel-hash helper in sync/sentinels.py MUST use only the
        stable subset (`STABLE_MCP_KEYS = ("name", "transport")`) for
        hashing — the `raw` field can drift across CLI versions (v7.1
        micro-patch). This method returns raw so downstream can render it
        for debugging, but hashing must NEVER include it.

        Returns [] when SYNC_MCP not in capabilities.
        """

    # -- external title lookup --
    def lookup_external_title(self, external_id: str) -> str | None:
        """Title the CLI itself holds for `external_id`, or None.

        Blocking file IO by contract — the CLI's state store is a separate
        sqlite/JSON file, so callers must run this off the event loop and
        outside any open transaction.

        Only called when EXTERNAL_TITLE is in capabilities; adapters without
        it need not implement this at all.
        """

    # -- skills sync --
    def skills_dir(self) -> Path | None:
        """Directory the CLI scans for skills, or None if unsupported.
        Claude: `<home_dir>/skills`. Codex: None as of 2026-07."""

    def list_skills(self) -> list[dict[str, Any]]:
        """Enumerate skills currently visible to the CLI.

        Shape: `[{"name": str, "path": str, "description": str}]`. Reads
        each SKILL.md's frontmatter. Returns [] when skills_dir() is None.
        """

    def list_skills_full(self) -> list[dict[str, Any]]:
        """SyncAgent-facing enumeration of skills including body_md.

        Same shape as `list_skills()` plus:
        - `body_md`: full SKILL.md contents (frontmatter + prose);
        - `file_count`: number of bundle files beside SKILL.md;
        - `bundle_hash`: hash over SKILL.md AND every bundle file's
          (rel_path, mode, content) — so a missing or chmod-stripped
          helper script registers as a difference.

        Bundle *contents* are deliberately NOT included: this runs on every
        agent tick, and a skill can carry a hundred files. Use
        `read_skill_bundle()` when the bytes are actually needed.

        Returns [] when SYNC_SKILLS is not in capabilities.
        """

    def read_skill_bundle(self, name: str) -> dict[str, Any] | None:
        """Read one skill off disk in full, bundle included.

        Shape: `{"name", "description", "body_md", "files": [BundleFile],
        "skipped": [str]}`. `files` excludes SKILL.md itself (that is
        `body_md`). `skipped` carries one line per omitted file — oversize
        or unreadable — so the caller can surface it rather than shipping a
        silently-partial bundle.

        Returns None when the skill does not exist on this agent.
        """

    def write_simple_skill(self, spec: dict[str, Any]) -> None:
        """DEPRECATED — use `write_skill_bundle()`.

        Kept because the adapter idempotency contract names it explicitly
        and out-of-tree adapters may still call it. Delegates to
        `write_skill_bundle()` with an empty bundle, which writes SKILL.md
        and prunes nothing.
        """

    def write_skill_bundle(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Materialise a whole skill directory: SKILL.md plus every file.

        `spec` shape:
          {"name": str, "description": str, "body_md": str,
           "files": [{"rel_path": str, "content": bytes, "mode": int}],
           "prune": {rel_path: sha256} | None}

        `prune` is the manifest previously written to THIS agent. Any path
        in it that is absent from `files` is deleted from the target. Files
        the adapter never wrote are left alone — pruning by "everything not
        in the new manifest" would eat whatever the user put there by hand.

        Permission bits from `mode` are applied after the write:
        `atomic_write_with_hash_guard` creates its temp file 0600, so
        without this an executable helper arrives unexecutable.

        Returns `{"written": [rel_path], "pruned": [rel_path]}`.

        Raises NotImplementedError when SYNC_SKILLS not in capabilities.
        Raises ExternalSkillSource when the target skill directory is a
        symlink (see `remove_skill`).
        """

    def remove_skill(self, name: str) -> None:
        """Delete `<skills_dir>/<name>/` recursively. Idempotent.

        Refuses to descend outside `skills_dir()` (path-traversal guard).
        A skill directory that is itself a symlink — the common shape when
        skills are checked out in a skill-book repo and linked in — is NOT
        followed: `ExternalSkillSource` is raised instead, so CSM never
        deletes through a link into somebody's git working tree.
        """

    # -- misc --
    def marker_syntax(self) -> MarkerSyntax:
        """Return the comment-style marker fences this adapter uses.

        Default is HTML comment style (`<!-- ... -->`), since claude and
        codex both operate on markdown memory files.
        """

    async def probe_sync_capabilities(self) -> frozenset[Capability]:
        """Runtime probe of the adapter's SYNC_* capability set.

        Called at boot and every poll_interval_sec tick IFF the adapter
        reports installed. Result is diffed against `self.capabilities`;
        on change a NotificationBus event fires and the cached status
        is updated.

        Implementation should call `<cli> <sub> --help` for each sub
        (`mcp`, `skill`, ...) with a 3s timeout via `run_cli()`.
        """


__all__ = [
    "Capability",
    "MarkerSyntax",
    "AdapterStatus",
    "AdapterArgvResult",
    "PostSpawnBindResult",
    "CLIAdapter",
    # Flag schema
    "FlagDescriptor",
    "SelectChoice",
    "CheckboxFlag",
    "SelectFlag",
    "ResumeFlag",
    "InfoBlock",
    "flag_to_dict",
]
