# CSM ↔ Codex multi-CLI branch — developer guide

Branch: **`llj_dev_codex`**
Worktree: **`<CSM_REPO>-codex/`** (parallel to the main
`<CSM_REPO>/` on `llj_dev`).

> **2026-07-25 update.** The initial P0-P7 bolted-on codex integration
> has been replaced by a **first-class multi-agent adapter architecture**.
> The primary developer docs now live under `docs/backends/`:
>
> - `docs/backends/canonical_events.md` — the event contract every
>   adapter meets.
> - `docs/backends/adding_a_new_adapter.md` — step-by-step to add a
>   third CLI (gemini or whatever).
> - `docs/decisions/0004-multi-agent-adapter-layer.md` — ADR
>   explaining the decision + rejected alternatives.
>
> This document is kept as a sandbox / smoke-test recipe.

## Quickstart (from within the worktree)

```bash
cd <CSM_REPO>-codex
conda activate csm

# Source the sandbox. Exports:
#   CSM_CLAUDE_HOME  → /tmp/csm-codex-sandbox-<user>/fake_claude
#   CSM_CODEX_HOME   → /tmp/csm-codex-sandbox-<user>/fake_codex
#   CSM_DB_PATH      → sandbox sqlite (not the main csm.db)
#   CSM_PORT=8100
#   Adapters are enabled by default; CSM_ENABLE_<AGENT>=0 disables one.
#   CSM_SANDBOX_MODE=1
#   CODEX_HOME       → same as CSM_CODEX_HOME  (isolates the codex CLI itself)
#   PYTHONPATH       → prepends this worktree's backend
source scripts/csm-codex-dev-sandbox.sh

# ⚠️ New in v2: opt-in each adapter you want to use.
# Optional explicit overrides (normally unnecessary):
export CSM_ENABLE_CLAUDE=1
export CSM_ENABLE_CODEX=1

# Bring the sandbox DB up to head (runs the new v2 migration).
alembic upgrade head

# Optional: run tests.
pytest tests/unit
```

## Sandbox guard (v2)

`backend/csm/main.py::_require_sandbox_on_codex_branch` now iterates
`adapter_registry.all()` — every registered adapter's `home_dir()` is
checked against `~/.<default_home_name>`. Adding a third adapter
automatically extends the guard; no code change needed.

Bailout condition: if the marker file `.codex-dev-branch` exists at
`settings.project_root`, boot refuses when ANY adapter's `home_dir()`
resolves to real user config. Sourcing `scripts/csm-codex-dev-sandbox.sh`
sets the env vars that redirect each adapter to `/tmp/csm-.../fake_*/`.

**Remove `.codex-dev-branch` before merging** — main will refuse to
start otherwise.

## Smoke: end-to-end HTTP session with codex

After `source scripts/csm-codex-dev-sandbox.sh` + `alembic upgrade head`
+ `./scripts/start.sh`:

```bash
# Copy your real auth so the sandboxed codex can log in.
cp ~/.codex/auth.json "$CODEX_HOME/auth.json"

# Confirm registry sees both adapters as usable.
curl -s http://127.0.0.1:8100/api/backends | jq

# Check preferences seed (should say default_agent=claude, first_run=true
# on a fresh sandbox DB — because the seed migration ran with
# has_completed_first_run=1 for backwards compat; the /preferences endpoint
# re-seeds with False when no row exists yet).
curl -s http://127.0.0.1:8100/api/preferences | jq

# Set default to claude, complete the wizard.
curl -s -X PUT http://127.0.0.1:8100/api/preferences \
  -H 'content-type: application/json' -H 'x-csm-client: 1' \
  -d '{"default_agent":"claude","has_completed_first_run":true}' | jq

# Spawn a codex session via explicit override.
curl -s -X POST http://127.0.0.1:8100/api/sessions \
  -H 'content-type: application/json' -H 'x-csm-client: 1' \
  -d '{
    "agent":"codex",
    "cwd":"/tmp",
    "initial_prompt":"print hello"
  }' | jq

# List sessions — the row should have `agent: "codex"`.
curl -s -H 'x-csm-client: 1' http://127.0.0.1:8100/api/sessions | \
    jq '.[] | {id, agent, status, pid}'

# Stop when done.
./scripts/stop.sh
```

## Cleaning up

```bash
# Wipe the sandbox
rm -rf /tmp/csm-codex-sandbox-$USER

# Remove the worktree (when this branch is done or merged)
cd <CSM_REPO>
git worktree remove <CSM_REPO>-codex
git branch -d llj_dev_codex   # or -D if merging via another route
```
