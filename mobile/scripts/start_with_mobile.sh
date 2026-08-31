#!/usr/bin/env bash
# Start uvicorn against csm.main:app with the mobile /m/ mount attached.
#
# Contract: the main repo's scripts/start.sh remains the desktop-only
# entrypoint. This wrapper is the ONLY way to enable the mobile SPA
# without touching backend/csm/main.py.
#
# Usage:
#   ./mobile/scripts/start_with_mobile.sh                  # 127.0.0.1:8000
#   ./mobile/scripts/start_with_mobile.sh 0.0.0.0 8000     # custom bind (LAN)
#   CSM_SKIP_FRONTEND_BUILD=1 ./mobile/scripts/start_with_mobile.sh
#   CSM_MOBILE_FOREGROUND=1   ./mobile/scripts/start_with_mobile.sh   # no daemon
#
# PID + log files live under mobile/ so they don't clash with the main
# repo's csm.pid / csm.log. Trust boundary is the SSH tunnel (ADR-0001), so
# the default bind is 127.0.0.1 — pass 0.0.0.0 explicitly to expose on LAN.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"

MOBILE_DIST="mobile/frontend/dist"
MOBILE_PID="mobile/csm-mobile.pid"
MOBILE_LOG="mobile/csm-mobile.log"

echo "==> CSM Mobile wrapper"
echo "    REPO_ROOT = $REPO_ROOT"
echo "    HOST:PORT = $HOST:$PORT"
echo "    dist      = $MOBILE_DIST"

if [[ -f "$MOBILE_DIST/index.html" ]]; then
    echo "==> Mobile SPA dist present, skipping build."
elif [[ "${CSM_SKIP_FRONTEND_BUILD:-}" == "1" ]]; then
    echo "!! CSM_SKIP_FRONTEND_BUILD=1 but $MOBILE_DIST/index.html missing." >&2
    echo "!! Build first: (cd mobile/frontend && npm install && npm run build)" >&2
    exit 2
else
    echo "==> Building mobile SPA (this only happens once per fresh clone)..."
    (
        cd mobile/frontend
        if [[ ! -d node_modules ]]; then
            npm install
        fi
        npm run build
    )
fi

if command -v lsof >/dev/null 2>&1; then
    if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "!! Port $PORT already in use — refuse to start." >&2
        echo "!! kill the offender first (see: lsof -iTCP:$PORT -sTCP:LISTEN)" >&2
        exit 3
    fi
fi

echo "==> uvicorn --factory mobile.backend_patch:_factory"
echo "    ws-ping-interval=30 ws-ping-timeout=10 (SSH-tunnel WS zombie mitigation)"

if [[ "${CSM_MOBILE_FOREGROUND:-}" == "1" ]]; then
    echo "==> foreground mode (Ctrl-C to stop; no PID file written)"
    exec uvicorn \
        --factory mobile.backend_patch:_factory \
        --host "$HOST" \
        --port "$PORT" \
        --ws-ping-interval 30 \
        --ws-ping-timeout 10
fi

# Daemonize so stop_mobile.sh can find it via the PID file.
nohup uvicorn \
    --factory mobile.backend_patch:_factory \
    --host "$HOST" \
    --port "$PORT" \
    --ws-ping-interval 30 \
    --ws-ping-timeout 10 \
    >"$MOBILE_LOG" 2>&1 &
mobile_pid=$!
echo "$mobile_pid" >"$MOBILE_PID"
disown "$mobile_pid" 2>/dev/null || true

echo "==> started (pid $mobile_pid)"
echo "    log:  $MOBILE_LOG   (tail -f to follow)"
echo "    pid:  $MOBILE_PID"
echo "    open: http://$HOST:$PORT/m/"
echo "    stop: ./mobile/scripts/stop_mobile.sh"
