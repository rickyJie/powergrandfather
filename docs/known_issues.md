# Known issues / v2 deferrals

These were explicitly scoped out of v1 per the architectural cut list.

## Functional gaps

- **No Onboarding Agent.** Task definitions are validated by YAML schema only; no LLM-driven completeness check at registration time.
- **No Supervisor Agent.** `auto_needs_review` notification type exists in the enum but is never auto-emitted.
- **No quota% estimation.** Token dashboard shows absolute usage only; alert rules are user-configured absolute thresholds. Reason: historical-hit sample (n≈9 over 6 days) was too sparse for a reliable denominator.
- **No predictive token alerts.** Same reason as above; only fires on absolute threshold cross.
- **No Lark IM sink.** Notification Bus has only In-app WebSocket sink. The sink interface is pluggable; adding Lark is a single new file.
- **No do-not-disturb window.** All notifications fire immediately.
- **No `{var}` substitution in prompt templates.** Task launcher uses literal `prompt_template`; the `parameters` dict is stored on Run but not interpolated.
- **No calendar view for schedules.** Lists only; cron string is the input.

## Runtime gaps

- **Terminal WebSocket has no auto-reconnect** if backend restarts mid-session.
- **No backpressure on attached WebSockets.** A slow client could in theory block the reader loop. Acceptable for single-user local deployment.
- **No hourly rollup job.** `hourly_rollup` table exists for schema stability; not yet populated by a background job. Will be needed once raw events hit ≥7 days of accumulation.
- **No raw token event TTL.** Same as above.
- **`session_id` reconciliation between PTY spawn and JSONL filename is partial.** Today we know `Session.id` (our uuid) and `EventStream` knows `claude_session_id` (JSONL stem) but the binding step is not automated — `Session.claude_session_id` stays null until set explicitly.

## Operational gaps

- **mypy not run in CI.** Optional dep; installs from pypi.org rather than internal devpi.
- **No docker / systemd integration.** Run with `scripts/dev.sh` or `scripts/start.sh` (added in P11).
- **No metrics / Prometheus export.** Logs only.

## Sync v2 agent-driven (added 2026-08-03)

The agent-driven multi-agent config sync (v7 design + v7.1 micro-patch)
is now feature-complete: backend + UI. As of 2026-08-04 the Sync UI was
consolidated into `Settings > Sync` (`/settings?section=sync`) —
standalone `/sync` route + sidebar `Y` icon retired; `/sync` now
redirects to the Settings section. Curl remains available for all
endpoints (see `docs/USAGE.md` § Sync v2). Remaining caveats:

- **No progress modal.** `POST /agent-tick` runs synchronously (the
  Run sync now button waits for completion; typically < 20s with the
  default haiku model). `GET /agent-runs/{id}` includes `live_phase`
  when the run is the currently-active one — the UI could poll it for
  a proper progress bar, deferred for now.
- **`phase2_done` ledger status is legacy-only.** The v7 §3 merged
  Phase 2.5+3 no longer produces this status on the happy path.
  `replay_pending_fanout_ledger` still handles it for compatibility
  with in-flight v6 rows.
- **~~Codex skills sync unavailable.~~** Resolved. codex-cli 0.145.0
  ships `~/.codex/skills/<name>/SKILL.md` — the same directory
  convention claude uses — so `CodexAdapter` gates `SYNC_SKILLS` on
  `skills_dir()` being present rather than on a CLI probe (codex has no
  `skill` subcommand to probe). Its built-in `.system/` skills are
  excluded from enumeration so they're never adopted as user skills.
- **Skill bundles land, but symlinked skill dirs are skipped by
  design** (2026-08-30). Sync now materialises the whole skill
  directory, not just SKILL.md. The one case it deliberately refuses is
  a target `<skills_dir>/<name>` that is itself a **symlink** — the
  usual shape when skills are checked out in a skill-book repo and
  linked into `~/.claude/skills`. Writing or pruning through the link
  would edit that git working tree with no record that CSM did it, so
  the push records `DriftReason.EXTERNAL_SOURCE` and returns
  `SKIPPED`. Reading is unaffected — the poller follows the link, so a
  symlinked skill still hashes clean and does not show up as drift on
  its own. The skip appears only when something actually pushes to that
  agent (a skill edit, a migrate, a reingest). Read it as "CSM is not
  managing this copy", not as a failure. To make one CSM-managed,
  replace the symlink with a real directory; to stop seeing the skip,
  drop that agent from the skill's `share_scope`.
- **Bundle exclusions are policy, not detection.** Files are filtered
  by a fixed junk list (`__pycache__`, `.git`, `*.pyc`, …) plus an
  optional per-skill `.csmsyncignore`. CSM does not try to infer which
  files a skill "really needs", so a skill that keeps generated
  artefacts beside its source (e.g. a `reports/` directory) will sync
  them until the user adds an ignore file. Caps are 1 MiB per file and
  200 files per skill; exceeding either is reported in the API
  response (`skipped_files`) or raises `BundleTooLarge` — never a
  silent truncation, because a bundle that looks complete and isn't is
  the exact failure this subsystem was built to end.
- **`sync_agent_run.phase` never records `failed`.** Live phase can
  observe collecting / deciding / applying / done. If the SyncAgent
  parse fails or Anthropic errors out, phase transitions to `done`
  with `error` populated instead. Downstream UIs should render on
  `error is not None` rather than a distinct phase enum.
- **v1 `DriftPoller` still runs alongside v2.** v2 opt-in is per-module
  via `sync_config.sync_mode='agent'`; modules left in `'lock'` mode
  continue using the v1 rule-driven poller. Both subsystems coexist
  intentionally so users can migrate incrementally.
- **Cold-start batching triggers > 400 resources.** Splits by module
  into 3 sub-ticks with `parent_run_id` linkage. If your CSM DB grows
  past 400 total resources (unlikely for single-user), the first
  cold-start tick will auto-batch. Env override: `CSM_SYNC_BATCH_THRESHOLD`.
