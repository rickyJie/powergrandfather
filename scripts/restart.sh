#!/usr/bin/env bash
# One-shot restart: stop → alembic upgrade → (optional frontend rebuild) → start.
# Any additional args are forwarded to start.sh (e.g. HOST PORT).
#
# Usage:
#   ./scripts/restart.sh                     # stop, migrate, start on defaults
#   ./scripts/restart.sh 127.0.0.1 8000      # custom host/port for start
#   ./scripts/restart.sh --rebuild-frontend  # also `npm run build` before start
#   ./scripts/restart.sh --skip-migrate      # skip alembic upgrade (dangerous)
#
# Unlike start.sh (which silently swallows alembic errors so a broken
# migration doesn't wedge boot), this script fails loud on migration
# errors — you're explicitly asking for a full restart, so a schema
# problem should stop you before uvicorn comes back up thinking the DB
# is fine.

set -e

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

# ---- parse flags (positional args pass through to start.sh) ----
REBUILD_FRONTEND=0
SKIP_MIGRATE=0
FORWARD_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --rebuild-frontend) REBUILD_FRONTEND=1 ;;
    --skip-migrate)     SKIP_MIGRATE=1 ;;
    --help|-h)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) FORWARD_ARGS+=("$arg") ;;
  esac
done

echo "[restart] ==== stop ===="
./scripts/stop.sh

# Activate conda env for alembic + any conda-linked libs uvicorn will
# inherit. Fine to noop if the operator is already inside csm env.
if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  # shellcheck disable=SC1091
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate csm
fi

if [ "$SKIP_MIGRATE" -eq 0 ]; then
  echo "[restart] ==== alembic upgrade head ===="
  if ! alembic upgrade head; then
    echo "[restart] alembic upgrade FAILED — refusing to start with unmigrated DB" >&2
    echo "[restart] fix migration then re-run ./scripts/restart.sh" >&2
    exit 1
  fi
else
  echo "[restart] --skip-migrate: alembic upgrade skipped"
fi

if [ "$REBUILD_FRONTEND" -eq 1 ]; then
  echo "[restart] ==== npm run build ===="
  ( cd frontend && npm run build )
fi

echo "[restart] ==== start ===="
./scripts/start.sh "${FORWARD_ARGS[@]}"

echo "[restart] ==== done ===="
