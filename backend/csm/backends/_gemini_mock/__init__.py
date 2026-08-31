"""Mock GeminiAdapter for M9.6 zero-frontend-change acceptance test.

This adapter is registered ONLY when `CSM_ENABLE_GEMINI_MOCK=1` is set in
env. It DOES NOT actually spawn any gemini CLI (there is no gemini
integration yet). Its sole purpose is to prove that adding a new adapter
requires ZERO frontend file changes:

    - Register a new class (this one)
    - Backend `flags_schema()` returns declarative UI
    - Frontend's <AdapterFlagsPanel>, <AgentBadge>, <AgentSelector>,
      first-run wizard, Settings page all render it correctly WITHOUT
      any per-adapter branching.

If any frontend view special-cases on `agent == "gemini"`, this smoke
test will fail visibly (colour missing, flags panel empty, etc).
"""
from csm.backends._gemini_mock.adapter import GeminiMockAdapter

__all__ = ["GeminiMockAdapter"]
