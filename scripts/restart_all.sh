#!/usr/bin/env bash
#
# One-shot restart of the whole console: stop whatever is running, rebuild both
# frontends, and start the SINGLE uvicorn process that co-serves the desktop
# console (/) AND the phone companion (/m/) against one csm.db.
#
# Why this exists: the mobile wrapper must run inside the `csm` conda env (the
# csm.* imports need the editable install) and must be the ONLY backend process
# (single-instance DB lock). Forgetting `conda activate csm` was the #1 footgun
# ("ModuleNotFoundError: No module named 'csm'").
#
# Usage:
#   ./scripts/restart_all.sh                 # 127.0.0.1:8000 (loopback — use SSH tunnel)
#   ./scripts/restart_all.sh 0.0.0.0 8000    # bind LAN (exposes IP)
#   CSM_SKIP_BUILD=1 ./scripts/restart_all.sh    # fast restart, reuse existing dist
#   CSM_CONDA_ENV=csm ./scripts/restart_all.sh   # override conda env name
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"
CONDA_ENV="${CSM_CONDA_ENV:-csm}"

log() { echo "==> $*"; }
warn() { echo "!!  $*" >&2; }

# ── 1. Activate the csm conda env (csm.* imports depend on it) ───────────────
if [[ "${CONDA_PREFIX:-}" != *"/envs/${CONDA_ENV}" ]]; then
  if ! command -v conda >/dev/null 2>&1; then
    for c in "$HOME/miniconda3" "$HOME/anaconda3" /opt/conda; do
      if [[ -f "$c/etc/profile.d/conda.sh" ]]; then
        # shellcheck disable=SC1091
        source "$c/etc/profile.d/conda.sh"; break
      fi
    done
  fi
  log "conda activate ${CONDA_ENV}"
  conda activate "${CONDA_ENV}"
fi
python -c "import csm" 2>/dev/null || {
  warn "csm not importable in this env ($(which python)). Run: pip install -e '.[dev]'"
  exit 1
}

# ── 2. Stop whatever is running (both the plain backend + the mobile wrapper) ─
log "stopping any running instance"
./scripts/stop.sh || true
./mobile/scripts/stop_mobile.sh || true

# ── 3. Rebuild frontends (skippable). Build failures warn but don't block the
#       restart — a rough in-progress change shouldn't brick the server. ──────
if [[ "${CSM_SKIP_BUILD:-}" != "1" ]]; then
  log "building desktop frontend (frontend/)"
  ( cd frontend && npm run build ) || warn "desktop build FAILED — serving previous dist"
  log "building mobile frontend (mobile/frontend/)"
  ( cd mobile/frontend && npm run build ) || warn "mobile build FAILED — serving previous dist"
else
  log "CSM_SKIP_BUILD=1 — reusing existing dist"
fi

# ── 4. Start the single process that serves both / and /m/ ──────────────────
log "starting mobile wrapper on ${HOST}:${PORT}"
# The wrapper skips its own build when mobile/frontend/dist is present (step 3
# just built it); on a fresh checkout with no dist it falls back to building.
./mobile/scripts/start_with_mobile.sh "$HOST" "$PORT"

# ── 5. Verify both surfaces answer ──────────────────────────────────────────
log "verifying …"
sleep 4
for path in "/" "/m/"; do
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "http://127.0.0.1:${PORT}${path}" || echo 000)"
  if [[ "$code" == "200" ]]; then
    echo "    OK   ${path} -> ${code}"
  else
    warn "  ${path} -> ${code} (check mobile/csm-mobile.log)"
  fi
done

echo
log "done. desktop: http://localhost:${PORT}/   mobile: http://localhost:${PORT}/m/"
log "stop with: ./mobile/scripts/stop_mobile.sh"
