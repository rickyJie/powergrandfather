#!/usr/bin/env bash
# csm_launch_mission.sh — thin helper for POST /api/missions/launch.
#
# Args:
#   $1 — workflow_name
#   $2 — params JSON file path (defaults to empty {})
#
# Emits mission_id on stdout (single line) on success.
# Exits non-zero + error to stderr on failure.

set -euo pipefail

wf="${1:?workflow_name required}"
params_file="${2:-}"

BASE_URL="${CSM_BASE_URL:-http://127.0.0.1:8000}"

params_json="{}"
if [ -n "$params_file" ] && [ -f "$params_file" ]; then
    params_json=$(cat "$params_file")
fi

body=$(python3 - "$wf" "$params_json" << 'PY'
import json, sys
name, raw = sys.argv[1], sys.argv[2]
try:
    params = json.loads(raw)
except Exception as e:
    print(f"bad params JSON: {e}", file=sys.stderr)
    sys.exit(2)
print(json.dumps({"workflow_name": name, "params": params}))
PY
)

resp=$(curl -sf -X POST "${BASE_URL}/api/missions/launch" \
    -H 'content-type: application/json' \
    -d "$body")

python3 - "$resp" << 'PY'
import json, sys
d = json.loads(sys.argv[1])
mid = d.get("id") or ""
if not mid:
    print("no id in launch response", file=sys.stderr)
    sys.exit(3)
print(mid)
PY
