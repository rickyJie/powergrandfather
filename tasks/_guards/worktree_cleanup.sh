#!/usr/bin/env bash
# worktree_cleanup.sh <source_repo> <worktree_path>
#
# Remove the worktree at <worktree_path>. Also deletes the csm-test/<mission>
# branch it was on, if that branch exists and points only to this worktree.
#
# Non-destructive: never touches other branches, never rewrites history.

set -euo pipefail

src="${1:?source_repo required}"
dst="${2:?worktree_path required}"

if [ ! -d "$dst" ]; then
    echo "worktree_cleanup: $dst does not exist; nothing to do" >&2
    exit 0
fi

# Capture the branch name before removal so we can delete it afterwards.
branch=$(git -C "$dst" symbolic-ref --short HEAD 2>/dev/null || true)

git -C "$src" worktree remove --force "$dst" >&2

if [ -n "$branch" ] && [[ "$branch" == csm-test/* ]]; then
    git -C "$src" branch -D "$branch" >&2 || true
fi

echo "worktree_cleanup: removed $dst (branch $branch)" >&2
