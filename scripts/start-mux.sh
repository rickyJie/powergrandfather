#!/usr/bin/env bash
# Same-port HTTP+HTTPS mode: uvicorn (HTTPS) on an internal loopback port,
# `scripts/proto_mux.py` on the public port doing per-connection TLS-vs-
# plain-HTTP detection and 301-redirecting the plain-HTTP visitors.
#
# Solves the "user types http://ip:8000/... and gets ERR_SSL_PROTOCOL_ERROR"
# issue (local:fc98b162). Existing `scripts/start.sh` (direct uvicorn bind)
# is untouched; use this one when you want same-port mux.
#
# Usage: ./scripts/start-mux.sh [public_host] [public_port] [internal_https_port]
#   default: 0.0.0.0 8000 18443
#
# Requires: secrets/csm-cert.pem + secrets/csm-key.pem (run gen-cert.sh first).

set -e

cd "$(dirname "$0")/.."
PROJECT_ROOT="$(pwd)"

PUBLIC_HOST="${1:-0.0.0.0}"
PUBLIC_PORT="${2:-8000}"
INTERNAL_HTTPS_PORT="${3:-18443}"

PIDFILE_UVICORN="$PROJECT_ROOT/csm.pid"
PIDFILE_MUX="$PROJECT_ROOT/csm-mux.pid"
LOGFILE_UVICORN="$PROJECT_ROOT/csm.log"
LOGFILE_MUX="$PROJECT_ROOT/csm-mux.log"

if [ -f "$PIDFILE_UVICORN" ] && kill -0 "$(cat "$PIDFILE_UVICORN")" 2>/dev/null; then
  echo "[start-mux] uvicorn already running (pid=$(cat "$PIDFILE_UVICORN"))"
  exit 0
fi

if [ -f "$HOME/miniconda3/etc/profile.d/conda.sh" ]; then
  source "$HOME/miniconda3/etc/profile.d/conda.sh"
  conda activate csm
fi

alembic upgrade head >/dev/null 2>&1 || true

if [ ! -d "$PROJECT_ROOT/frontend/dist" ]; then
  echo "[start-mux] frontend/dist missing; build first: cd frontend && npm install && npm run build"
fi

CERT="$PROJECT_ROOT/secrets/csm-cert.pem"
KEY="$PROJECT_ROOT/secrets/csm-key.pem"
if [ ! -f "$CERT" ] || [ ! -f "$KEY" ]; then
  echo "[start-mux] cert/key missing under secrets/ — run ./scripts/gen-cert.sh first"
  exit 1
fi

# 1) uvicorn — HTTPS on the internal loopback port.
export CSM_HOST="127.0.0.1"
export CSM_PORT="$INTERNAL_HTTPS_PORT"
echo "[start-mux] uvicorn HTTPS on 127.0.0.1:$INTERNAL_HTTPS_PORT (log: $LOGFILE_UVICORN)"
nohup uvicorn csm.main:app --host 127.0.0.1 --port "$INTERNAL_HTTPS_PORT" --log-level info \
  --ssl-keyfile "$KEY" --ssl-certfile "$CERT" \
  > "$LOGFILE_UVICORN" 2>&1 &
echo $! > "$PIDFILE_UVICORN"
echo "[start-mux] uvicorn pid=$(cat "$PIDFILE_UVICORN")"

# 2) proto_mux — public port, per-connection dispatch.
echo "[start-mux] proto_mux on ${PUBLIC_HOST}:${PUBLIC_PORT} → https / 301 (log: $LOGFILE_MUX)"
nohup python "$PROJECT_ROOT/scripts/proto_mux.py" \
  --host "$PUBLIC_HOST" \
  --public-port "$PUBLIC_PORT" \
  --internal-https-port "$INTERNAL_HTTPS_PORT" \
  > "$LOGFILE_MUX" 2>&1 &
echo $! > "$PIDFILE_MUX"
echo "[start-mux] proto_mux pid=$(cat "$PIDFILE_MUX")"

echo "[start-mux] ready — both https://<host>:${PUBLIC_PORT}/ and http://<host>:${PUBLIC_PORT}/ work"
