"""Application configuration loaded from environment with sensible defaults."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from csm.core import paths as _paths


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CSM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Project root (default: parent of backend/csm/)
    project_root: Path = Path(__file__).resolve().parents[2]

    # SQLite path (relative to project_root if not absolute)
    db_path: Path = Path("csm.db")

    # Loopback-only by default: the console is reachable only from the host
    # itself (open it over an SSH tunnel — VSCode port-forward or `ssh -L` —
    # so it's never exposed on the LAN). Set CSM_HOST=0.0.0.0 (or pass a HOST
    # arg to scripts/start.sh) to bind all interfaces for direct LAN access.
    host: str = "127.0.0.1"
    port: int = 8000
    access_token: str | None = None

    # Public base URL used in Lark push messages so the link is clickable
    # from a phone that isn't on the LAN. Format: "https://<host-or-ip>:<port>"
    # (no trailing slash). Empty → LarkSink falls back to
    # "https://localhost:{port}", which only works if the recipient is on
    # the CSM host. Set to your LAN IP or DNS name for real usage.
    public_base_url: str = ""

    # TLS (optional) — when both files exist, `scripts/start.sh` boots uvicorn
    # with --ssl-keyfile / --ssl-certfile and the browser sees a secure context
    # (unblocking navigator.clipboard etc. over LAN IP). Missing / non-existent
    # files silently fall back to plain HTTP. Generate with scripts/gen-cert.sh.
    # Paths are resolved relative to project_root.
    ssl_certfile: Path = Path("secrets/csm-cert.pem")
    ssl_keyfile: Path = Path("secrets/csm-key.pem")
    # Whether uvicorn actually boots with TLS. MUST match how the server is
    # launched: scripts/start.sh only passes --ssl-* when CSM_ENABLE_TLS=1.
    # The hook-callback scheme keys off this (not merely "do cert files exist"):
    # certs can sit in secrets/ while the server runs plain HTTP (mobile default,
    # since the SSH tunnel is the trust boundary). A cert-file-only check made
    # every loopback hook POST dial https:// against a plaintext port → TLS
    # handshake garbage ("Invalid HTTP request received") and dead hooks.
    enable_tls: bool = False

    def resolved_ssl_paths(self) -> tuple[Path, Path] | None:
        cert = self.ssl_certfile if self.ssl_certfile.is_absolute() else self.project_root / self.ssl_certfile
        key = self.ssl_keyfile if self.ssl_keyfile.is_absolute() else self.project_root / self.ssl_keyfile
        if cert.is_file() and key.is_file():
            return cert, key
        return None

    # Claude artifacts root for Event Stream tailing.
    # Default: honors CSM_CLAUDE_HOME (sandbox), else ~/.claude/projects.
    # Direct override still possible via CSM_CLAUDE_PROJECTS_DIR.
    claude_projects_dir: Path = Field(default_factory=_paths.claude_projects_dir)

    # Codex artifacts root for future rollout tailing (P4).
    # Default: honors CSM_CODEX_HOME (sandbox), else ~/.codex/sessions.
    codex_sessions_dir: Path = Field(default_factory=_paths.codex_sessions_dir)

    # Feature flag: enable Codex backend support. Registered adapters are
    # enabled by default across both spawn and event-ingestion paths; set
    # CSM_ENABLE_CODEX=0 to explicitly disable Codex everywhere.
    enable_codex: bool = True

    # File-preview endpoint: base URL that `s3://<bucket>/<key>`
    # URIs get rewritten to for the "click OSS link in terminal" flow.
    # Result URL: f"{oss_base_url}/{bucket}/{key}".
    #
    # Empty by default — an object-store endpoint is deployment-specific, and
    # baking one in both leaks the author's infrastructure and silently points
    # every other deployment at a host it can't reach. Set `CSM_OSS_BASE_URL`
    # to enable the redirect; while unset, the endpoint returns 503.
    oss_base_url: str = ""

    # File-preview size cap. Files larger than this render as an error
    # page with a "Download" link instead of an inline preview — protects
    # the browser from choking on multi-MB code files. In bytes.
    file_preview_max_bytes: int = 2 * 1024 * 1024  # 2 MiB
    # Higher cap for `.html` / `.htm` preview specifically: the rendered
    # branch loads through a streaming iframe (no full read into memory)
    # so it tolerates larger files gracefully. Only the Source tab pays
    # the "read + pygments highlight" cost. Big data-viz reports (Plotly
    # / Chart.js dumps) routinely land in the 3-8 MB range — cap them at
    # 10 MiB rather than the generic 2 MiB text limit. In bytes.
    file_preview_html_max_bytes: int = 10 * 1024 * 1024  # 10 MiB

    # C6 — optional path allowlist for `/api/files/preview` and
    # `/api/files/raw`. Empty list = no restriction (current, deliberate
    # any-path behavior — see backend/csm/api/files.py module docstring).
    # Non-empty = each request's resolved path must be under one of these
    # roots or the endpoint returns 403. Opt-in for team / LAN
    # deployments where trust model is looser than single-user localhost.
    file_preview_allowed_roots: list[str] = []

    # Task definition YAML directory
    tasks_dir: Path = Path("tasks")

    # Event Stream tuning
    event_stream_poll_interval_sec: float = 5.0
    event_stream_watchdog_interval_sec: float = 60.0
    session_idle_minutes: int = 30

    # Session Manager
    ring_buffer_bytes: int = 1024 * 1024  # 1 MiB
    # Durable tail snapshots for ended-session review. Each file is named
    # `<csm-session-id>.ansi` and contains at most `ring_buffer_bytes`.
    # Relative paths resolve from project_root.
    session_output_dir: Path = Path(".csm/session-output")
    session_stop_grace_sec: int = 5
    # Working directory for CSM's OWN headless `claude -p` helpers (agent-alert
    # escalation / check-script generation). These spawn a real CLI, so they
    # write a real transcript into `~/.claude/projects/<encoded cwd>/`. Left to
    # inherit the backend's cwd they land in the SAME project folder as the
    # user's interactive sessions in this repo — and the cwd-fallback rebind in
    # NotificationBus then adopts that orphan transcript onto a live session
    # row, which is how a token alert ended up rendered inside an unrelated
    # chat (2026-08-30). A dedicated cwd gives them their own project folder,
    # so no cwd-keyed lookup can ever confuse the two. Relative paths resolve
    # from project_root.
    internal_agent_cwd: Path = Path(".csm/internal-agent")
    # Interval between batched flushes of live sessions' last_activity_at
    # timestamps to SQLite. Reader loops stamp in-memory; this task drains
    # them in one transaction per tick — keeps SQLite commits bounded when
    # many sessions are active.
    activity_flush_interval_sec: float = 15.0
    # SessionManager: periodic reap-loop cadence for ORPHANED rows whose
    # pid has since died. Without this, a session marked orphaned at
    # startup stays orphaned in the UI even after the process exits —
    # user has to restart CSM (or click Purge) to un-stick it. Set to 0
    # to disable the loop entirely (tests).
    orphan_reap_interval_sec: float = 30.0
    # Override the binary used for AUTO sessions (default `["claude"]`).
    # Useful for tests/dev to substitute bash. Parsed with shlex.
    claude_argv: str | None = None

    @property
    def resolved_session_output_dir(self) -> Path:
        path = self.session_output_dir
        return path if path.is_absolute() else self.project_root / path

    # AutomationRunner: seconds to wait after an assistant `end_turn` event
    # before force-stopping the AUTO session. Re-armed on every assistant
    # turn within the window so multi-turn tool chains aren't cut short.
    auto_grace_sec: float = 10.0

    # WorkflowOrchestrator T6 rescuer cadence — scan the mission table this
    # often to detect stalled / orphaned missions. Matches other Run polling
    # cadences; keeps stray full-table SELECT cost bounded (single-user,
    # mission table stays small).
    rescue_interval_sec: float = 30.0

    # Hard ceiling for workflow shell checks. A check that waits for stdin or
    # deadlocks must not pin its poll loop forever.
    workflow_shell_check_timeout_sec: float = 60.0

    # Notification
    notif_dedup_window_sec: int = 5

    # Token rollup / TTL (F7)
    rollup_tick_interval_sec: float = 3600.0
    # FALLBACK raw_token_event retention window (days). The live value is now
    # runtime-managed in `user_preference.raw_event_retention_days` (editable via
    # PUT /api/preferences, read by the RollupWorker each tick); this setting is
    # only used to seed / when that row can't be read. 0 = keep forever; a
    # positive int deletes raw rows older than N days after they've been rolled
    # up (hourly_rollup accumulates independently, so trend charts are unaffected).
    # Default 180 ≈ half a year — long enough for per-session/task drill-down and
    # safely above the ~35-day floor that monthly budgets need.
    raw_event_retention_days: int = 180

    # Usage-live poller: how often to spawn `claude /usage` in the background
    # to refresh the 5h/weekly quota card. 0 disables the scheduler (manual
    # refresh via API still works). Default 30 min matches user's config.
    usage_poll_interval_min: int = 30
    usage_probe_cwd: Path = Path.home() / ".csm" / "usage_probe"
    # Wall-clock timeout guard for pexpect probe. asyncio.to_thread cannot kill
    # the worker on timeout, so a hung pexpect read would permanently occupy a
    # thread-pool slot (default pool = 10). This bound lets the async poller
    # return None and unblock the next tick even if the worker leaks.
    usage_probe_timeout_sec: int = 60

    # ---- Spawned-session proxy env ----
    # Users typically configure `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` in
    # their zsh RC file. `scripts/start.sh` runs uvicorn under bash (or
    # under a service manager), so zshrc is never sourced and the child
    # `claude` / `codex` processes spawned by SessionManager inherit a
    # proxy-less env — network calls to api.anthropic.com then fail.
    #
    # Resolution: on startup CSM sniffs `$SHELL -ic 'export -p'` and
    # extracts whitelisted proxy vars, then layers those (plus an optional
    # `~/.csm/proxy.env` override file) into every session's spawn env.
    # See `csm.modules.session_manager.env` for the full policy.
    proxy_auto_sniff: bool = True
    proxy_env_file: Path = Path.home() / ".csm" / "proxy.env"
    proxy_sniff_timeout_sec: float = 3.0

    # ---- Sync v2 agent-driven decision execution ----
    # The SyncAgent's LLM decision step runs as a real AUTO session spawned
    # through SessionManager under the user's *default agent* (claude / codex
    # login), NOT a direct Anthropic API call — so it needs no
    # ANTHROPIC_API_KEY and works for whatever CLI the user is signed into.
    # Each decide() spawns a throwaway session in a scratch dir under this
    # root: it reads `sync_input.md` (policy + state) and writes its decisions
    # to `decisions.json`, which CSM then parses. Mirrors the file-based
    # output-harvest that workflow stages already use (dodges the 2000-char
    # assistant_text truncation + argv length limits).
    sync_decide_cwd: Path = Path.home() / ".csm" / "sync_decide"
    # Wall-clock ceiling for one decide session (spawn → agent reads the state
    # → writes the file → we harvest). Generous: a cold CLI turn over a large
    # state (many skills/instructions) legitimately takes minutes. On timeout
    # the session is force-stopped and the tick records an error.
    sync_decide_timeout_sec: int = 300

    # ---- Multi-agent config sync (P0 v3) ----
    # DriftPoller outer tick cadence — detects CSM DB ⇄ on-agent drift every
    # N seconds (e.g. a user hand-editing ~/.claude/CLAUDE.md or ~/.codex/
    # AGENTS.md outside CSM). Lowered from 60s → 10s for near-real-time
    # detection of external edits (zero-dependency alternative to an inotify
    # watcher). Cost per tick is small — a sha256 over each enrolled agent's
    # memory file plus a cheap `mcp list`; raise via CSM_SYNC_DRIFT_TICK_SEC
    # if the CLI-spawn cost is noticeable. Smaller = more real-time, more CPU.
    sync_drift_tick_sec: float = 10.0

    # ---- Supervisor agent ----
    # Model for post-run review LLM calls. Cheap/fast Haiku by default.
    supervisor_model: str = "claude-haiku-4-5"
    # Forcibly disable the SupervisorAgent even when ANTHROPIC_API_KEY is set.
    supervisor_disabled: bool = False

    # ---- Lark notifications (DEPRECATED env-var slots) ----
    # Config moved to the `lark_settings` singleton table (readable /
    # writable via GET/PUT /api/settings/lark, no restart needed). These
    # env vars are read ONCE by the Alembic migration to seed the row
    # for existing deployments; LarkSink itself no longer reads them.
    # Kept as tolerant `str | None` (even for numeric fields) so a
    # malformed operator env — e.g. `CSM_LARK_DEDUP_SEC=abc` — cannot
    # prevent Settings() from loading and block CSM boot / migration.
    # Slated for removal in the next release.
    lark_dnd_hours: str = ""
    lark_tz: str | None = None
    lark_notify_chat_id: str | None = None
    lark_notify_user_id: str | None = None
    lark_dedup_sec: str | None = None

    def resolved_db_path(self) -> Path:
        """Absolute path to the SQLite file (relative paths resolve from project_root)."""
        path = self.db_path
        if not path.is_absolute():
            path = self.project_root / path
        return path

    def resolved_db_url(self) -> str:
        """Build async SQLAlchemy URL relative to project_root if path is relative."""
        return f"sqlite+aiosqlite:///{self.resolved_db_path()}"

    def resolved_internal_agent_cwd(self) -> Path:
        """Absolute, existing cwd for CSM's own headless `claude -p` helpers.

        Created on demand — callers pass the result straight to
        `create_subprocess_exec(cwd=...)`, which fails on a missing directory.
        """
        path = self.internal_agent_cwd
        if not path.is_absolute():
            path = self.project_root / path
        path.mkdir(parents=True, exist_ok=True)
        return path


settings = Settings()
