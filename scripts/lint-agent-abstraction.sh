#!/usr/bin/env bash
# lint-agent-abstraction.sh
#
# CI guard for the multi-agent v2 refactor: fail the build if any
# domain-layer code special-cases on adapter name. The whole point of the
# CLIAdapter abstraction is that "if agent == 'claude'" / "backend == codex"
# never leaks out of csm/backends/.
#
# Allowed locations:
#   - backend/csm/backends/                     — the abstraction itself
#   - tests/                                    — test fixtures often name adapters
#   - alembic/versions/                         — migration seed values
#   - backend/csm/modules/session_manager/manager.py  — legacy branches
#       (marked for M6 deletion; see the code comments)
#   - backend/csm/api/sessions.py               — one legacy `elif effective_agent == "codex"`
#       still there for the enable_codex flag guard; M4 will replace it
#       with a registry-driven check.
#
# Any other match is a leak and must be fixed by delegating to the adapter.
#
# Usage:
#   ./scripts/lint-agent-abstraction.sh
# Exit code 0 = clean, 1 = leaks found.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Patterns to grep: comparison of an `agent` or `backend` variable to a
# literal adapter name. This is precise — it won't accidentally catch
# `stage.kind == "claude"` (workflow-stage discriminator, unrelated to
# adapter abstraction) or other legitimate string equality checks.
#
# Matches:
#   agent == "claude"      / agent == 'codex'
#   backend == "claude"    / backend == 'codex'
#   agent != "claude"      etc.
#   effective_agent == "claude"     — anything ending in `agent`
#   body.backend == "codex"          — anything ending in `.backend`
PATTERNS=(
    '\b[a-z_]*agent\s*[!=]=\s*["'"'"'](claude|codex)["'"'"']'
    '\b[a-z_]*backend\s*[!=]=\s*["'"'"'](claude|codex)["'"'"']'
)

# Files whose leaks are intentionally tolerated (transitional).
ALLOWLIST_REGEX='^(backend/csm/backends/|tests/|alembic/versions/|backend/csm/modules/session_manager/manager\.py|backend/csm/api/sessions\.py|scripts/lint-agent-abstraction\.sh)'

leaks_found=0
for pattern in "${PATTERNS[@]}"; do
    # grep -R with -E for extended regex; --include limits to .py.
    while IFS= read -r line; do
        # Skip if the file is on the allowlist.
        file="${line%%:*}"
        rel="${file#$REPO_ROOT/}"
        if echo "$rel" | grep -Eq "$ALLOWLIST_REGEX"; then
            continue
        fi
        echo "LEAK: $line"
        leaks_found=1
    done < <(
        grep -RnE --include='*.py' "$pattern" backend/csm/ 2>/dev/null || true
    )
done

if [ "$leaks_found" -eq 1 ]; then
    echo ""
    echo "===================================================================="
    echo "Agent-abstraction leak detected. See LEAK lines above."
    echo "Domain code MUST NOT special-case on adapter name — use the"
    echo "CLIAdapter methods (adapter.build_argv / adapter.scan_events /"
    echo "adapter.capabilities) instead. See docs/backends/adding_a_new_adapter.md."
    echo "===================================================================="
    exit 1
fi
echo "OK: no agent-abstraction leaks outside allowed transitional paths."
