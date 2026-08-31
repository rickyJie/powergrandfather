# mobile/tests/backend/

Isolated pytest suite for mobile backend patch. **Populated Phase 1+**.

## Run

```bash
pytest mobile/tests/backend/                    # mobile-only
pytest mobile/tests/backend/ tests/             # mobile + desktop regression
```

## Isolation contract

- No shared `conftest.py` with `tests/` — this suite ships its own
- Does not import from `tests/*` (only `csm.*` production code)
- Safe to delete this directory without touching desktop test suite
- CI can run this as a separate job

## Planned cases (per Phase)

- Phase 1: `test_mount_fallback.py` (/m/ SPA routing, real file, no /api leak)
- Phase 2: `test_agentchat_ws_flow.py`, `test_session_lifecycle.py`
- Phase 3: `test_missions_lifecycle.py`, `test_notifications_ws.py`
- Phase 4: `test_feedback_submit.py`, `test_readonly_endpoints.py`
- Phase 5: `test_concurrent_writes.py` + regression checks
