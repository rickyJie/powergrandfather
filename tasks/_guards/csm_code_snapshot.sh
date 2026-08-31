#!/usr/bin/env bash
# csm_code_snapshot.sh — pre-fix git snapshot for supervisor code-change loop.
#
# Args:
#   $1 — phase number (int, 1-4)
#   $2 — fix_iter (int)
#
# Behaviour:
#   git stash push -u --include-untracked -m "csm-fix-p${phase}-i${iter}-<ts>"
#   Emits JSON to stdout on success:
#     {"stashed": true, "stash_ref": "stash@{0}", "message": "..."}
#   If working tree already clean (nothing to stash), emits:
#     {"stashed": false, "message": "clean_wt"}
#   Exit 0 in both cases; non-zero only on git error.
#
# Usage from SUPERVISOR.md:
#   result=$(bash tasks/_guards/csm_code_snapshot.sh <phase> <iter>)
#   → check .stashed, record .stash_ref if true
#   → then apply patch via Edit, then run pytest
#   → if pytest fails: git stash pop <stash_ref> && git reset --hard HEAD
#   → if pytest passes: git commit + record commit sha for later revert-by-sha

set -euo pipefail

phase="${1:?phase required (int)}"
iter="${2:?fix_iter required (int)}"
ts=$(date -u +%Y%m%dT%H%M%SZ)
msg="csm-fix-p${phase}-i${iter}-${ts}"

cd "$(dirname "$0")/../.."

# Any uncommitted changes at all?
if git diff --quiet HEAD -- && [ -z "$(git ls-files --others --exclude-standard)" ]; then
    printf '{"stashed":false,"message":"clean_wt","ts":"%s"}\n' "$ts"
    exit 0
fi

git stash push -u --include-untracked -m "$msg" >/dev/null
ref="stash@{0}"
printf '{"stashed":true,"stash_ref":"%s","message":"%s","ts":"%s"}\n' \
    "$ref" "$msg" "$ts"
