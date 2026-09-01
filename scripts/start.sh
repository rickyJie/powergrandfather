#!/usr/bin/env bash
# Production start: run uvicorn serving the API + built frontend statics.
# Usage: ./scripts/start.sh [host] [port]
# Requires conda env "csm" with deps installed and frontend already built (frontend/dist).

set -e

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

HOST="${1:-127.0.0.1}"
PORT="${2:-8000}"
export CSM_HOST="$HOST"
export CSM_PORT="$PORT"

PIDFILE="$PROJECT_ROOT/csm.pid"
LOGFILE="$PROJECT_ROOT/csm.log"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "[start] already running (pid=$(cat "$PIDFILE"))"
  exit 0
fi

# Activate conda env if available.
if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate csm
fi

# Ensure DB migrated.
alembic upgrade head >/dev/null 2>&1 || true

# Ensure frontend built.
if [ ! -d "$PROJECT_ROOT/frontend/dist" ]; then
  echo "[start] frontend/dist missing; build first: cd frontend && npm install && npm run build"
fi

# TLS: OFF by default. The mobile companion connects over an SSH tunnel whose
# trust boundary IS the tunnel (ADR-0001), and its WebView loads plain
# http://localhost:<port>/m/ — a plaintext request onto a TLS socket fails the
# handshake, so serving HTTPS here makes the phone unusable. We therefore run
# plain HTTP unless TLS is explicitly opted into with CSM_ENABLE_TLS=1 (e.g. a
# desktop-only LAN box that wants navigator.clipboard.readText() for right-click
# paste over a LAN IP — a secure-context-gated API). When enabled, a cert+key
# pair must be present.
CERT="$PROJECT_ROOT/secrets/csm-cert.pem"
KEY="$PROJECT_ROOT/secrets/csm-key.pem"
SSL_ARGS=()
SCHEME="http"
if [ "${CSM_ENABLE_TLS:-}" = "1" ] && [ -f "$CERT" ] && [ -f "$KEY" ]; then
  SSL_ARGS=(--ssl-keyfile "$KEY" --ssl-certfile "$CERT")
  SCHEME="https"
fi

STARTUP_TIMEOUT_SEC="${CSM_STARTUP_TIMEOUT_SEC:-15}"
if ! [[ "$STARTUP_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]]; then
  echo "[start] invalid CSM_STARTUP_TIMEOUT_SEC=$STARTUP_TIMEOUT_SEC (expected positive integer)" >&2
  exit 1
fi

echo "[start] uvicorn on ${SCHEME}://$HOST:$PORT (logs: $LOGFILE)"
if [ "$SCHEME" = "http" ]; then
  echo "[start] plain HTTP (mobile /m/ over SSH tunnel needs this). Desktop-only LAN box wanting right-click paste: CSM_ENABLE_TLS=1 ./scripts/start.sh (after ./scripts/gen-cert.sh)"
fi
nohup uvicorn csm.main:app --host "$HOST" --port "$PORT" --log-level info \
  "${SSL_ARGS[@]}" \
  > "$LOGFILE" 2>&1 &
UVICORN_PID=$!
echo "$UVICORN_PID" > "$PIDFILE"
echo "[start] pid=$UVICORN_PID"

# Readiness gate: a PID can still be alive while uvicorn is finishing startup,
# then die on a late bind/import failure. Do not report success until the real
# API answers. Python is already a hard dependency, so the probe does not add
# a curl requirement to the installer.
case "$HOST" in
  0.0.0.0) PROBE_HOST="127.0.0.1" ;;
  ::|\[::\]) PROBE_HOST="[::1]" ;;
  *) PROBE_HOST="$HOST" ;;
esac
HEALTH_URL="${SCHEME}://${PROBE_HOST}:${PORT}/api/health"
STARTUP_DEADLINE=$((SECONDS + STARTUP_TIMEOUT_SEC))
HEALTHY=0
while kill -0 "$UVICORN_PID" 2>/dev/null && (( SECONDS < STARTUP_DEADLINE )); do
  if CSM_STARTUP_HEALTH_URL="$HEALTH_URL" python -c '
import os, ssl, urllib.request
url = os.environ["CSM_STARTUP_HEALTH_URL"]
request = urllib.request.Request(url, headers={"X-CSM-Client": "1"})
context = ssl._create_unverified_context() if url.startswith("https://") else None
with urllib.request.urlopen(request, timeout=1, context=context) as response:
    if response.status != 200:
        raise SystemExit(1)
' >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 0.25
done

if [ "$HEALTHY" != "1" ]; then
  echo "[start] server did not become healthy within ${STARTUP_TIMEOUT_SEC}s — dumping last 20 lines of $LOGFILE:" >&2
  echo "---8<--- csm.log tail ---8<---" >&2
  tail -n 20 "$LOGFILE" >&2 || true
  echo "---8<--- end ---8<---" >&2
  if kill -0 "$UVICORN_PID" 2>/dev/null; then
    kill -TERM "$UVICORN_PID" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  exit 1
fi
echo "[start] healthy at $HEALTH_URL ✓"
