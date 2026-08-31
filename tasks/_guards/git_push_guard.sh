#!/usr/bin/env bash
# git_push_guard.sh <repo_or_worktree> <remote> <refspec>
#
# Push guard for real_*.workflow.yaml simulations. Default behaviour is to
# REFUSE the push and instead write a "would push …" line to stderr. Set
# CSM_ALLOW_PUSH=1 to actually push.
#
# Even when CSM_ALLOW_PUSH=1, the refspec must land on a branch prefixed with
# `csm-test/` — otherwise the push is refused unconditionally to protect
# production remotes.
#
# On success (real or dry-run), prints a JSON line:
#   {"pushed": bool, "remote": "...", "refspec": "...", "reason": "..."}

set -euo pipefail

repo="${1:?repo_or_worktree required}"
remote="${2:?remote required}"
refspec="${3:?refspec required}"

allow="${CSM_ALLOW_PUSH:-0}"

# Extract the destination branch from the refspec, whether it's "branch",
# "src:dst", or "HEAD:refs/heads/dst".
dst_branch="${refspec##*:}"
dst_branch="${dst_branch#refs/heads/}"

if [[ "$dst_branch" != csm-test/* ]]; then
    echo "git_push_guard: refusing — destination branch '$dst_branch' is not under csm-test/" >&2
    printf '{"pushed":false,"remote":"%s","refspec":"%s","reason":"non-csm-test-branch"}\n' \
        "$remote" "$refspec"
    exit 4
fi

if [ "$allow" != "1" ]; then
    echo "git_push_guard: dry-run (CSM_ALLOW_PUSH != 1) — would push $refspec to $remote" >&2
    printf '{"pushed":false,"remote":"%s","refspec":"%s","reason":"dry-run"}\n' \
        "$remote" "$refspec"
    exit 0
fi

git -C "$repo" push "$remote" "$refspec" >&2
printf '{"pushed":true,"remote":"%s","refspec":"%s","reason":"pushed"}\n' \
    "$remote" "$refspec"
