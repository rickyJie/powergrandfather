#!/usr/bin/env bash
# csm-codex-dev-sandbox.sh
# Bootstrap an isolated sandbox for developing the Codex-backend integration
# on branch llj_dev_codex. NEVER touches ~/.claude or ~/.codex.
#
# Usage (must `source`, not exec, so exports stick):
#   source scripts/csm-codex-dev-sandbox.sh
#   ./scripts/dev.sh    # or ./scripts/start.sh
#
# What it creates (all under /tmp):
#   /tmp/csm-codex-sandbox-<user>/
#     fake_claude/          -> CSM_CLAUDE_HOME
#       projects/
#       settings.json       (empty stub)
#     fake_codex/           -> CSM_CODEX_HOME
#       sessions/
#       config.toml         (empty stub)
#     db/csm-codex-dev.db   -> CSM_DB_PATH
#     logs/
#
# What it does NOT do:
#   - Copy anything FROM your real ~/.claude or ~/.codex
#   - Modify anything IN your real ~/.claude or ~/.codex
#   - Write to the main csm.db in the repo root

set -eo pipefail

SANDBOX_ROOT="/tmp/csm-codex-sandbox-${USER:-$(whoami)}"

# Sourced-guard: works in both bash (${BASH_SOURCE[0]}) and zsh (${(%):-%N})
_csm_sourced=0
if [ -n "${BASH_SOURCE-}" ]; then
    [ "${BASH_SOURCE[0]}" != "${0}" ] && _csm_sourced=1
elif [ -n "${ZSH_EVAL_CONTEXT-}" ]; then
    case "$ZSH_EVAL_CONTEXT" in *:file*) _csm_sourced=1 ;; esac
fi
if [ "$_csm_sourced" -eq 0 ]; then
    echo "ERROR: must be sourced. Use:  source scripts/csm-codex-dev-sandbox.sh" >&2
    return 1 2>/dev/null || exit 1
fi
unset _csm_sourced

mkdir -p \
    "$SANDBOX_ROOT/fake_claude/projects" \
    "$SANDBOX_ROOT/fake_codex/sessions" \
    "$SANDBOX_ROOT/db" \
    "$SANDBOX_ROOT/logs"

# Empty stubs (never overwrite real user files)
[[ -f "$SANDBOX_ROOT/fake_claude/settings.json" ]] || echo '{}' > "$SANDBOX_ROOT/fake_claude/settings.json"
[[ -f "$SANDBOX_ROOT/fake_codex/config.toml" ]]   || echo '# sandbox stub'   > "$SANDBOX_ROOT/fake_codex/config.toml"

export CSM_CLAUDE_HOME="$SANDBOX_ROOT/fake_claude"
export CSM_CODEX_HOME="$SANDBOX_ROOT/fake_codex"
export CSM_DB_PATH="$SANDBOX_ROOT/db/csm-codex-dev.db"
export CSM_PORT="8100"
export CSM_ENABLE_CODEX="1"
export CSM_SANDBOX_MODE="1"

# Force the codex worktree to win over the editable-install .pth pointing at
# the main worktree. Without this, `import csm.*` resolves to the OTHER
# worktree and our edits on this branch are invisible.
_CSM_CODEX_BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%N}}")/.." && pwd)/backend"
export PYTHONPATH="${_CSM_CODEX_BACKEND}${PYTHONPATH:+:$PYTHONPATH}"
unset _CSM_CODEX_BACKEND

# Codex itself respects CODEX_HOME — point it at the sandbox too so any
# ad-hoc `codex doctor` / `codex exec` in this shell can't touch real ~/.codex.
export CODEX_HOME="$SANDBOX_ROOT/fake_codex"

# Wrapper: refuse to run `codex` without CODEX_HOME set (belt-and-suspenders).
codex() {
    if [ -z "${CODEX_HOME-}" ]; then
        echo "[sandbox] ERROR: CODEX_HOME not set — refusing to run real codex." >&2
        return 1
    fi
    command codex "$@"
}

echo "[sandbox] CSM_CLAUDE_HOME=$CSM_CLAUDE_HOME"
echo "[sandbox] CODEX_HOME=$CODEX_HOME  (codex CLI wrapper enforces this)"
echo "[sandbox] CSM_CODEX_HOME=$CSM_CODEX_HOME"
echo "[sandbox] CSM_DB_PATH=$CSM_DB_PATH"
echo "[sandbox] CSM_PORT=$CSM_PORT"
echo "[sandbox] CSM_ENABLE_CODEX=$CSM_ENABLE_CODEX"
echo "[sandbox] CSM_SANDBOX_MODE=$CSM_SANDBOX_MODE"
echo ""
echo "[sandbox] ready. Real ~/.claude and ~/.codex are UNTOUCHED."
echo "[sandbox] to clean up: rm -rf $SANDBOX_ROOT"
