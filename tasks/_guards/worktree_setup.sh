#!/usr/bin/env bash
# worktree_setup.sh <source_repo> <target_worktree_path> [<base_branch>]
#
# Create a git worktree at <target_worktree_path> from <source_repo>, based on
# <base_branch> (default: current HEAD of source repo). New branch name is
# derived from CSM_MISSION_ID for traceability.
#
# Emits JSON to stdout on success:
#   {"worktree_path": "...", "branch": "...", "base_sha": "..."}
#
# Exits non-zero with a human-readable error on stderr on failure.

set -euo pipefail

src="${1:?source_repo required}"
dst="${2:?target_worktree_path required}"
base="${3:-HEAD}"

mission_id="${CSM_MISSION_ID:-manual-$$}"
branch="csm-test/${mission_id}"

if [ ! -d "$src/.git" ] && [ ! -f "$src/.git" ]; then
    echo "worktree_setup: $src is not a git repo" >&2
    exit 2
fi

if [ -e "$dst" ]; then
    echo "worktree_setup: destination $dst already exists" >&2
    exit 3
fi

mkdir -p "$(dirname "$dst")"

# Resolve base SHA up front for the JSON output.
base_sha=$(git -C "$src" rev-parse "$base")

git -C "$src" worktree add -b "$branch" "$dst" "$base" >&2

printf '{"worktree_path":"%s","branch":"%s","base_sha":"%s"}\n' \
    "$dst" "$branch" "$base_sha"
