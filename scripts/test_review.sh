#!/usr/bin/env bash
# scripts/test_review.sh
#
# End-to-end smoke test for the workflow review module.
#
# 1) Builds a fixture .workflow/ in /tmp (good and bad variants)
# 2) Creates a TaskDefinition pointing at it via POST /api/tasks
# 3) Triggers POST /api/tasks/{id}/review (synchronous, 10-90s)
# 4) Pretty-prints the report so you can see every rule's verdict
# 5) Demonstrates override on a soft-rule fail
#
# Prereqs:
#   - CSM running on :8000 (./scripts/start.sh)
#   - `claude` CLI installed and authenticated (the reviewer spawns `claude -p`)
#   - `jq` available
#
# Usage:
#   ./scripts/test_review.sh              # full flow with GOOD fixture
#   ./scripts/test_review.sh bad          # flow with BAD fixture (expect failed)
#   ./scripts/test_review.sh hard-fail    # missing required files (no LLM call)

set -euo pipefail

VARIANT="${1:-good}"
BASE="${CSM_BASE_URL:-http://127.0.0.1:8000}"
STAMP="$(date +%s)"
FIXTURE_DIR="/tmp/csm_review_test_${STAMP}"
TASK_NAME="review_test_${STAMP}"

cyan() { printf '\033[36m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
red() { printf '\033[31m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }

need() { command -v "$1" >/dev/null 2>&1 || { red "missing: $1"; exit 1; }; }
need curl
need jq

cyan "=== Step 1: build fixture .workflow/ (variant=${VARIANT}) ==="
mkdir -p "${FIXTURE_DIR}/.workflow"

case "${VARIANT}" in
  good)
    cat > "${FIXTURE_DIR}/.workflow/README.md" <<'EOF'
# Daily Metric Refresh

## What this flow does
Once a day, recompute the model evaluation metrics on the latest dataset
snapshot and write a report.

## Steps (agent perspective)
1. Activate conda env `csm` (already present, no install).
2. Run `python eval.py --dataset latest --out report.json`.
3. Verify `report.json` is valid JSON and contains `accuracy` >= 0.85.
4. Write `final_report.md` summarizing accuracy + delta vs yesterday.

## External deps
- conda env `csm`
- Read-only access to `/data/snapshots/`

## Error handling
- Step 2 fails non-zero: retry once after 30s, then bail.
- Step 3 fails (bad JSON or low accuracy): write `error.log` and exit 1.
- No human intervention required at any point.

## Expected runtime
~2 minutes. Anything over 10 minutes = stuck.
EOF
    cat > "${FIXTURE_DIR}/.workflow/done_criteria.md" <<'EOF'
# Done Criteria

ALL must hold:
- [ ] `report.json` exists and is valid JSON
- [ ] `report.json` contains field `accuracy` >= 0.85
- [ ] `final_report.md` exists and contains string "status: ok"
- [ ] Exit code of the eval script is 0

Failure:
- [ ] `error.log` exists and is non-empty
EOF
    cat > "${FIXTURE_DIR}/.workflow/outputs.md" <<'EOF'
# Outputs

| Path | Type | Note |
|---|---|---|
| `report.json` | json | main result, accuracy + per-class metrics |
| `final_report.md` | md | human summary, supervisor reads this |
| `error.log` | log | only present on failure |
EOF
    ;;
  bad)
    cat > "${FIXTURE_DIR}/.workflow/README.md" <<'EOF'
# Some Flow

Run the thing. If it works, great.

Wait for user confirmation before proceeding to step 2.
Then have someone manually verify the output looks right.
EOF
    cat > "${FIXTURE_DIR}/.workflow/done_criteria.md" <<'EOF'
# Done

It's done when it feels done. Look at the report and decide.
EOF
    ;;
  hard-fail)
    # only .workflow/ with a random file → R2/R3 fail
    echo "irrelevant" > "${FIXTURE_DIR}/.workflow/notes.md"
    ;;
  *)
    red "unknown variant: ${VARIANT}"
    exit 2
    ;;
esac

ls -la "${FIXTURE_DIR}/.workflow/"

cyan "\n=== Step 2: POST /api/tasks (create TaskDefinition) ==="
CREATE_BODY=$(jq -nc \
  --arg name "${TASK_NAME}" \
  --arg cwd "${FIXTURE_DIR}" \
  --arg prompt "do the thing per .workflow/README.md" \
  '{name:$name, cwd:$cwd, prompt:$prompt, description:"review smoke test"}')
RESP=$(curl -fsS -X POST "${BASE}/api/tasks" \
  -H 'Content-Type: application/json' \
  -d "${CREATE_BODY}")
TASK_ID=$(echo "${RESP}" | jq -r '.id')
green "created task ${TASK_ID} (name=${TASK_NAME})"
echo "${RESP}" | jq '{id, name, cwd, review_status}'

cyan "\n=== Step 3: POST /api/tasks/${TASK_ID}/review (synchronous) ==="
yellow "this can take 10-90s; the reviewer spawns claude -p"
REVIEW=$(curl -fsS -X POST "${BASE}/api/tasks/${TASK_ID}/review")
echo "${REVIEW}" | jq '.report | {final_status, summary, error, hard, soft, overrides}'

FINAL=$(echo "${REVIEW}" | jq -r '.report.final_status')
case "${FINAL}" in
  passed) green "→ PASSED"; ;;
  passed_with_overrides) green "→ PASSED_WITH_OVERRIDES"; ;;
  failed) red "→ FAILED"; ;;
  *) yellow "→ ${FINAL}"; ;;
esac

if [ "${VARIANT}" = "bad" ] && [ "${FINAL}" = "failed" ]; then
  cyan "\n=== Step 4: override a soft-rule fail (demo) ==="
  FAIL_RULE=$(echo "${REVIEW}" | jq -r '.report.soft[] | select(.status=="fail") | .rule' | head -1)
  if [ -n "${FAIL_RULE}" ] && [ "${FAIL_RULE}" != "null" ]; then
    yellow "overriding ${FAIL_RULE} with a (contrived) reason"
    OVR=$(curl -fsS -X POST "${BASE}/api/tasks/${TASK_ID}/review/override" \
      -H 'Content-Type: application/json' \
      -d "{\"rule_id\":\"${FAIL_RULE}\",\"reason\":\"smoke-test override\"}")
    echo "${OVR}" | jq '.report | {final_status, overrides}'
  fi
fi

cyan "\n=== Cleanup ==="
echo "fixture left at: ${FIXTURE_DIR}"
echo "task left in DB: ${TASK_ID} (name=${TASK_NAME})"
echo "to remove:"
echo "  rm -rf ${FIXTURE_DIR}"
echo "  curl -X DELETE ${BASE}/api/tasks/${TASK_ID}"
