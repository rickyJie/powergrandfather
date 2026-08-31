#!/usr/bin/env bash
# Run backend + frontend dev servers in parallel.
# Requires conda env "csm" active for backend, and frontend deps installed.

set -e

cd "$(dirname "$0")/.."

PROJECT_ROOT="$(pwd)"
echo "[dev] project_root=$PROJECT_ROOT"

# Trap to kill children on exit
PIDS=()
cleanup() {
  echo "[dev] stopping (PIDS=${PIDS[*]})"
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Backend (uvicorn)
echo "[dev] starting backend on 127.0.0.1:8000"
( cd "$PROJECT_ROOT" && uvicorn csm.main:app --host 127.0.0.1 --port 8000 --reload --reload-dir backend ) &
PIDS+=($!)

# Frontend (vite)
echo "[dev] starting frontend on http://localhost:5173"
( cd "$PROJECT_ROOT/frontend" && npm run dev ) &
PIDS+=($!)

# Wait for either to exit
wait -n
