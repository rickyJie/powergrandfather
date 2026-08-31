#!/usr/bin/env bash
# Stop the uvicorn process launched by start_with_mobile.sh.
# SIGINT (5s) → SIGTERM (5s) → SIGKILL (5s) — mirrors main repo scripts/stop.sh.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

MOBILE_PID_FILE="mobile/csm-mobile.pid"

if [[ ! -f "$MOBILE_PID_FILE" ]]; then
    echo "No PID file at $MOBILE_PID_FILE — nothing to stop."
    exit 0
fi

pid=$(cat "$MOBILE_PID_FILE")
if ! kill -0 "$pid" 2>/dev/null; then
    echo "Stale PID file (pid $pid not alive), removing."
    rm -f "$MOBILE_PID_FILE"
    exit 0
fi

for sig in INT TERM KILL; do
    echo "==> Sending SIG$sig to $pid"
    kill -"$sig" "$pid" 2>/dev/null || true
    for _ in {1..5}; do
        sleep 1
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "==> Process $pid exited on SIG$sig"
            rm -f "$MOBILE_PID_FILE"
            exit 0
        fi
    done
done

echo "!! Process $pid still alive after SIGKILL — manual intervention needed." >&2
exit 1
