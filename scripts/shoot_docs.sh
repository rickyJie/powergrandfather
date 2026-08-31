#!/usr/bin/env bash
# Re-shoot every documentation screenshot against a disposable demo backend.
#
# The images in README.md must not contain real project names, paths, or
# people. This script therefore never touches your live csm.db: it seeds a
# throwaway database with fictional data (scripts/seed_demo.py), boots a
# second backend against it on a spare port, drives Playwright over it, and
# tears the whole thing down.
#
#   ./scripts/shoot_docs.sh              # shoot everything
#   ./scripts/shoot_docs.sh sessions     # only files matching "sessions"
#
# Requires: the `csm` conda env active, frontend/dist built, playwright
# chromium installed (`python -m playwright install chromium`).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# `--gif` also records docs/screenshots/demo.gif. Off by default: it needs
# ffmpeg, adds ~30s, and the stills are what most doc edits actually need.
WANT_GIF=0
if [[ "${1:-}" == "--gif" ]]; then
  WANT_GIF=1
  shift
fi

PORT="${PGF_SHOOT_PORT:-8899}"
# Session whose fabricated transcript the live PTY replays.
SID_HERO="11111111-1111-4111-8111-111111111111"
WORK="${PGF_SHOOT_WORK:-/tmp/pgf-demo}"
DB="$WORK/demo.db"
OUTPUT_DIR="$WORK/session-output"
TASKS_DIR="$WORK/tasks"
PREVIEW_DIR="/tmp/pgf-demo-preview"
# Empty dirs so EventStream has no real JSONL corpus to ingest — otherwise the
# tailer would pour your actual sessions into the demo database mid-shoot.
EMPTY_CLAUDE="$WORK/empty-claude-projects"
EMPTY_CODEX="$WORK/empty-codex-sessions"

if [[ ! -f frontend/dist/index.html ]]; then
  echo "frontend/dist/index.html missing — run (cd frontend && npm run build) first." >&2
  exit 1
fi

rm -rf "$WORK"
mkdir -p "$OUTPUT_DIR" "$TASKS_DIR" "$EMPTY_CLAUDE" "$EMPTY_CODEX" "$PREVIEW_DIR"

# A small, fictional source file for the file-preview screenshot.
cat > "$PREVIEW_DIR/pricing.ts" <<'TS'
import { multiply, subtotal, type Money } from "./money"
import { UnknownRegionError } from "./errors"
import type { Cart, CartItem, TaxRules } from "./types"

/** Price of a single line, before tax. */
export function lineSubtotal(item: CartItem): Money {
  return multiply(item.unitPrice, item.quantity)
}

/**
 * Tax owed for a cart under the given rule set.
 *
 * Extracted out of `CartSummary` so the bracket rules can be tested
 * without mounting a component.
 */
export function taxFor(cart: Cart, rules: TaxRules): Money {
  const bracket = rules.brackets.find((b) => b.matches(cart.region))
  if (!bracket) throw new UnknownRegionError(cart.region)
  return multiply(subtotal(cart), bracket.rate)
}

export function total(cart: Cart, rules: TaxRules): Money {
  return subtotal(cart).plus(taxFor(cart, rules))
}
TS

export CSM_DB_PATH="$DB"
export CSM_SESSION_OUTPUT_DIR="$OUTPUT_DIR"
export CSM_TASKS_DIR="$TASKS_DIR"
export CSM_CLAUDE_PROJECTS_DIR="$EMPTY_CLAUDE"
export CSM_CODEX_SESSIONS_DIR="$EMPTY_CODEX"
export CSM_HOST=127.0.0.1
export CSM_PORT="$PORT"
export CSM_SUPERVISOR_DISABLED=1
export CSM_SYNC_DISABLED=1
export CSM_ENABLE_TLS=0
# Keep the pollers idle for the life of the shoot.
export CSM_USAGE_POLL_INTERVAL_MIN=100000
export CSM_PORT_SCAN_INTERVAL_SEC=100000
# The live demo session runs a replay script, not `claude`.
export CSM_ALLOW_ARBITRARY_ARGV=1
# Per-line delay for the replay. Baked into the generated script rather than
# passed as an env var: the replay is forked by the BACKEND, and relying on
# what the session manager does or doesn't forward from its own environment is
# a needless dependency. 0.35s is roughly the cadence of a real agent turn —
# fast enough not to bore, slow enough to read. Stills-only runs use 0 so
# nothing waits.
REPLAY_DELAY=0
REPLAY_PASSES=1
if [[ "$WANT_GIF" == "1" ]]; then
  REPLAY_DELAY=0.35
  # 39 lines at 0.35s is ~14s of writing, and Playwright needs most of that
  # just to import, launch chromium and load the SPA — so a single pass is
  # over before the recorder attaches, and the clip captures a finished
  # terminal. Three passes keep output arriving across the whole window.
  REPLAY_PASSES=3
fi
export CSM_EVENT_STREAM_POLL_INTERVAL_SEC=100000

echo "▸ migrating $DB"
alembic upgrade head >/dev/null

echo "▸ seeding demo data"
python scripts/seed_demo.py \
  --db "$DB" \
  --session-output-dir "$OUTPUT_DIR" \
  --tasks-dir "$TASKS_DIR" >/dev/null

echo "▸ booting demo backend on 127.0.0.1:$PORT"
python -m uvicorn csm.main:app --host 127.0.0.1 --port "$PORT" \
  > "$WORK/demo.log" 2>&1 &
BACKEND_PID=$!
# Stand-ins for the demo sessions' child processes. Two reapers rewrite a
# live-looking session whose pid is dead to CRASHED — one on boot, one inside
# GET /api/sessions — so without a real pid behind each row, the very request
# that renders the Sessions page is what empties the Active tab. These sleeps
# exist only to be alive; they die with the trap below.
KEEPALIVE_PIDS=()
for _ in 1 2 3 4; do
  sleep 900 &
  KEEPALIVE_PIDS+=("$!")
done
LIVE_PIDS=$(IFS=,; echo "${KEEPALIVE_PIDS[*]}")

cleanup() {
  echo "▸ stopping demo backend ($BACKEND_PID)"
  kill "$BACKEND_PID" 2>/dev/null || true
  wait "$BACKEND_PID" 2>/dev/null || true
  for kp in "${KEEPALIVE_PIDS[@]}"; do
    kill "$kp" 2>/dev/null || true
  done
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  if curl -sf -m 2 -H 'X-CSM-Client: 1' "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
if ! curl -sf -m 2 -H 'X-CSM-Client: 1' "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
  echo "demo backend never came up; last 40 lines of $WORK/demo.log:" >&2
  tail -40 "$WORK/demo.log" >&2
  exit 1
fi

# The boot-time orphan reap has run by now. Re-apply the intended statuses and
# point each live row at one of the keepalive pids, so the request that renders
# the page finds them alive and leaves them be.
python scripts/seed_demo.py --db "$DB" --restore-live --live-pids "$LIVE_PIDS" >/dev/null

# One GENUINELY live session, so the terminal pane shows a real attached PTY
# rather than "this session has ended". The seeded rows can look alive in the
# list — status plus a live pid is all the list needs — but the terminal pane
# is driven by an actual websocket to an actual fd, and faking that is not
# possible from the database. So spawn one for real: a replay script that
# writes the fabricated transcript and then holds the PTY open.
REPLAY="$WORK/agent_replay.sh"
{
  echo '#!/usr/bin/env bash'
  # Emit the transcript a line at a time rather than cat-ing it. Still
  # instantaneous for the stills, but it gives the GIF recorder something that
  # actually moves — a static terminal photographed for 12 seconds is just a
  # heavier screenshot.
  echo "for _ in \$(seq 1 $REPLAY_PASSES); do"
  echo "  while IFS= read -r line; do"
  echo '    printf "%s\r\n" "$line"'
  echo "    sleep $REPLAY_DELAY"
  echo "  done < '$OUTPUT_DIR/$SID_HERO.ansi'"
  echo "done"
  echo 'exec sleep 900'
} > "$REPLAY"
chmod +x "$REPLAY"

LIVE_SID=$(curl -sf -m 10 -X POST "http://127.0.0.1:$PORT/api/sessions" \
  -H 'Content-Type: application/json' -H 'X-CSM-Client: 1' \
  -d "{\"cwd\": \"$WORK\", \"title\": \"Refactor checkout pricing\", \"argv\": [\"bash\", \"$REPLAY\"]}" \
  | python -c 'import json,sys; print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)

if [[ -n "$LIVE_SID" ]]; then
  echo "▸ live demo session $LIVE_SID"
  # The PTY had to start in a directory that exists; the header shows the cwd,
  # so rewrite it to the fictional repo path the rest of the demo uses. Display
  # only — the process is already running and never reads this column.
  python - "$DB" "$LIVE_SID" "$SID_HERO" <<'PYEOF'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
# The PTY had to start in a directory that exists; the header shows the cwd,
# so rewrite it to the fictional repo path the rest of the demo uses. Display
# only — the process is already running and never reads this column.
con.execute("update session set cwd = ? where id = ?",
            ("/home/dev/code/webapp", sys.argv[2]))
# The live session replays the seeded one's transcript under the same title,
# so without this the fleet shows the same work twice. Archive the seeded row
# rather than delete it: notifications, token rows and worktime intervals all
# reference its id, and a dangling FK would break other shots.
con.execute("update session set archived_at = datetime('now'), "
            "status = 'EXITED' where id = ?", (sys.argv[3],))
con.commit()
PYEOF
  sleep 2
  # Record BEFORE the stills. The replay writes for about 15s and the stills
  # take longer than that, so recording afterwards captures a terminal that
  # has already finished — which is just a screenshot that weighs 3 MB.
  if [[ "$WANT_GIF" == "1" ]]; then
    echo "▸ recording demo.gif"
    python scripts/shoot_demo_gif.py --base "http://127.0.0.1:$PORT" --live-sid "$LIVE_SID" || true
  fi
else
  echo "▸ warning: could not spawn a live session; the terminal pane will read as ended" >&2
fi

echo "▸ shooting"
if [[ $# -gt 0 ]]; then
  python scripts/shoot_docs.py --base "http://127.0.0.1:$PORT" --live-sid "$LIVE_SID" --only "$1"
else
  python scripts/shoot_docs.py --base "http://127.0.0.1:$PORT" --live-sid "$LIVE_SID"
fi

echo "▸ done → docs/screenshots/"
