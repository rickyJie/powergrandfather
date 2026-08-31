#!/usr/bin/env bash
# csm_restart.sh — detached restart-and-relaunch script for supervisor.
#
# Called from SUPERVISOR.md with `nohup … & disown` so it survives when
# scripts/stop.sh kills the CSM uvicorn (and thereby the supervisor's
# auto session, which may or may not be reaped by the process group).
#
# Args:
#   $1 — current_mission_id     (may be "none" if no mission active)
#   $2 — fix_commit_sha         (the sha we may need to revert if health fails)
#   $3 — next_workflow_name     (workflow to launch after restart, or "none")
#   $4 — next_params_json_file  (path to file containing JSON params, or "none")
#   $5 — phase (int)
#   $6 — result_file            (where this script writes its outcome)
#
# Result file JSON on completion:
#   {
#     "phase": N,
#     "canceled_mission": "..." | null,
#     "stopped": true|false,
#     "started": true|false,
#     "health_ok": true|false,
#     "reverted": true|false,
#     "revert_reason": "...",
#     "next_mission_id": "..." | null,
#     "escalation": "..." | null,
#     "finished_at": "ISO8601"
#   }

set -euo pipefail

cur_mission="${1:?current_mission_id required}"
fix_sha="${2:?fix_commit_sha required}"
next_wf="${3:?next_workflow_name required}"
next_params_file="${4:?next_params_json_file required}"
phase="${5:?phase required}"
result_file="${6:?result_file required}"

CSM_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
BASE_URL="http://127.0.0.1:8000"
SENTINEL="${CSM_ROOT}/.workflow/state/restart_in_progress"

log() { echo "[csm_restart $(date -u +%H:%M:%S)] $*" >&2; }

# Write sentinel FIRST so supervisor Branch A on the next tick can detect
# an in-flight restart and skip launching a new mission during the race.
mkdir -p "$(dirname "$SENTINEL")"
date -u +%Y-%m-%dT%H:%M:%SZ > "$SENTINEL"

# Accumulate result fields in a temp file, atomically rename at the end.
tmp=$(mktemp)
trap 'rm -f "$tmp"; rm -f "$SENTINEL"' EXIT

python3 - "$tmp" << PY
import json, sys
with open(sys.argv[1], "w") as f:
    json.dump({
        "phase": ${phase},
        "canceled_mission": None,
        "stopped": False,
        "started": False,
        "health_ok": False,
        "reverted": False,
        "revert_reason": "",
        "next_mission_id": None,
        "escalation": None,
        "finished_at": ""
    }, f)
PY

update_result() {
    python3 - "$tmp" "$@" << 'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    d = json.load(f)
for i in range(2, len(sys.argv), 2):
    k = sys.argv[i]
    v = sys.argv[i+1]
    # Try JSON parse, else keep as string
    try:
        d[k] = json.loads(v)
    except Exception:
        d[k] = v
with open(path, "w") as f:
    json.dump(d, f)
PY
}

# Step 1: Cancel current mission if any (so stage claude gets torn down).
if [ "$cur_mission" != "none" ] && [ -n "$cur_mission" ]; then
    log "cancel mission $cur_mission"
    if curl -sf -X POST "${BASE_URL}/api/missions/${cur_mission}/cancel" > /dev/null; then
        update_result canceled_mission "\"${cur_mission}\""
    else
        log "cancel failed (mission may already be terminal)"
    fi
    # Give CSM a moment to tear down the stage session cleanly.
    sleep 2
fi

# Step 2: Stop CSM.
log "stop CSM"
if bash "${CSM_ROOT}/scripts/stop.sh"; then
    update_result stopped true
else
    log "stop returned non-zero (may be already down)"
fi

sleep 3

# Step 3: Start CSM.
log "start CSM"
if bash "${CSM_ROOT}/scripts/start.sh" 127.0.0.1 8000; then
    update_result started true
else
    log "start.sh failed"
    update_result escalation "\"start_sh_failed\""
    update_result finished_at "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
    mv "$tmp" "$result_file"
    exit 3
fi

# Step 4: Health check loop (5s pre-sleep + 30 tries × 1s = 35s total).
# CSM lifespan needs to init EventStream → SessionManager → TokenAggregator
# → AlertEvaluator → BudgetEvaluator → PortRegistry → NotificationBus
# → AgentStore/ConvManager → TaskLoader → AutomationRunner
# → AutomationScheduler → SupervisorAgent → RollupWorker. Cold start is
# comfortably > 10 s on a busy box; 35 s window covers reasonable variance.
sleep 5
health_ok=false
for _ in $(seq 1 30); do
    if curl -sf "${BASE_URL}/api/health" 2>/dev/null | grep -q '"status":\s*"ok"'; then
        health_ok=true
        break
    fi
    sleep 1
done

if [ "$health_ok" = "true" ]; then
    update_result health_ok true
else
    log "health check failed after 10s"
    update_result health_ok false

    # Step 4b: revert the recorded fix commit (by sha, not HEAD).
    log "revert commit $fix_sha"
    if git -C "$CSM_ROOT" revert --no-edit "$fix_sha"; then
        update_result reverted true
        update_result revert_reason "\"health_fail_after_fix\""

        # Restart once more.
        bash "${CSM_ROOT}/scripts/stop.sh" || true
        sleep 3
        bash "${CSM_ROOT}/scripts/start.sh" 127.0.0.1 8000 || true

        sleep 5
        health_ok=false
        for _ in $(seq 1 30); do
            if curl -sf "${BASE_URL}/api/health" 2>/dev/null | grep -q '"status":\s*"ok"'; then
                health_ok=true
                break
            fi
            sleep 1
        done

        if [ "$health_ok" != "true" ]; then
            update_result escalation "\"revert_and_restart_still_unhealthy\""
            update_result finished_at "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
            mv "$tmp" "$result_file"
            exit 4
        fi
        update_result health_ok true
    else
        # Revert hit a merge conflict — abort so we don't leave conflict
        # markers in the tree that break the next tick's git operations.
        log "revert conflict; aborting"
        git -C "$CSM_ROOT" revert --abort 2>/dev/null || true
        update_result escalation "\"revert_failed_conflict_aborted\""
        update_result finished_at "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
        mv "$tmp" "$result_file"
        exit 5
    fi
fi

# Step 5: Launch next mission if requested.
if [ "$next_wf" != "none" ] && [ -n "$next_wf" ]; then
    log "launch next mission workflow=$next_wf"
    params_json="{}"
    if [ "$next_params_file" != "none" ] && [ -f "$next_params_file" ]; then
        params_json=$(cat "$next_params_file")
    fi
    body=$(python3 - "$next_wf" "$params_json" << 'PY'
import json, sys
name, params_raw = sys.argv[1], sys.argv[2]
try:
    params = json.loads(params_raw) if params_raw else {}
except Exception:
    params = {}
print(json.dumps({"workflow_name": name, "params": params}))
PY
)
    resp=$(curl -sf -X POST "${BASE_URL}/api/missions/launch" \
        -H 'content-type: application/json' \
        -d "$body" || echo '{}')
    new_id=$(python3 - "$resp" << 'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
    print(d.get("id") or "")
except Exception:
    print("")
PY
)
    if [ -n "$new_id" ]; then
        update_result next_mission_id "\"$new_id\""
        log "next mission launched: $new_id"
    else
        update_result escalation "\"next_launch_failed\""
    fi
fi

update_result finished_at "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
mv "$tmp" "$result_file"
log "done"
