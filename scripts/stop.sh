#!/usr/bin/env bash
# Stop a running PowerGrandFather backend (started via scripts/start.sh).
set -e

cd "$(dirname "$0")/.."
PIDFILE="$(pwd)/csm.pid"

if [ ! -f "$PIDFILE" ]; then
  echo "[stop] no pid file at $PIDFILE — nothing to stop"
  exit 0
fi

PID="$(cat "$PIDFILE")"
if ! kill -0 "$PID" 2>/dev/null; then
  echo "[stop] pid $PID is not running; removing stale pid file"
  rm -f "$PIDFILE"
  exit 0
fi

echo "[stop] sending SIGINT to pid=$PID"
kill -INT "$PID" || true

# Wait up to 10s for graceful shutdown.
for i in $(seq 1 20); do
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done

if kill -0 "$PID" 2>/dev/null; then
  echo "[stop] still alive after 10s, escalating to SIGTERM"
  kill -TERM "$PID" || true
  sleep 2
fi

if kill -0 "$PID" 2>/dev/null; then
  echo "[stop] still alive, SIGKILL"
  kill -KILL "$PID" || true
fi

rm -f "$PIDFILE"
echo "[stop] done"
