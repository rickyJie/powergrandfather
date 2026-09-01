# Known limits and deferred work (v0.9.3)

This page describes the current public snapshot. It is intentionally narrower
than a general-purpose agent platform.

## Product limits

- **Single-user only.** There is no account system, role model or tenant
  isolation. Keep the default loopback bind unless you have followed the
  README's remote-access guidance.
- **No git-worktree isolation.** Sessions operate directly in the selected
  working directory. PowerGrandFather does not create a branch or worktree per
  task.
- **No quota-percentage estimation.** The token dashboard reports observed
  usage and user-configured absolute thresholds. Provider quota denominators
  are not inferred from sparse rate-limit samples.
- **No predictive token alerts.** Alerts fire on configured thresholds rather
  than forecasting a future quota breach.
- **No hosted service or horizontal scaling.** The supported deployment shape
  is one FastAPI process and one local SQLite database.

## Operational limits

- **Public CI currently covers Ubuntu only.** macOS and Windows are not part of
  the v0.9.3 compatibility guarantee.
- **mypy is not run in CI.** It remains an optional development dependency.
- **No Docker or systemd packaging.** Use `scripts/dev.sh` or
  `scripts/start.sh`.
- **The Android release key is not public.** A fresh clone can build a
  debug-signed APK for side-loading, but it cannot upgrade an APK signed with
  the maintainer's release key.

## Implemented since the original cut list

Older copies of this document listed the following as deferred; they are
present in v0.9.3: Supervisor Agent review, Lark notifications with
do-not-disturb hours, schedule calendar, prompt parameter substitution,
terminal WebSocket reconnection, hourly token rollups with configurable raw
event retention, and a Prometheus-format metrics endpoint.

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
