# mobile/backend_patch/

FastAPI mount patch for the mobile SPA. **Populated in Phase 1** (`mount.py`).

## Design

`register(app)` idempotently attaches:
- `StaticFiles` mount at `/m/assets` → `mobile/frontend/dist/assets/`
- Catch-all `GET /m/{spa_path:path}` returning `index.html` for Vue Router history mode
- Real-file fallthrough (favicon etc.) via `is_file()` + `is_relative_to()` check

Route registration order is critical: mobile catch-all must be added
**before** the desktop `spa_fallback` (registered by main repo `csm.main`
when `frontend/dist/index.html` exists). The wrapper `start_with_mobile.sh`
calls `register(app)` **after** import of `csm.main` — this works because
FastAPI matches routes in registration order, and mobile catch-all is
declared with narrower prefix `/m/{...}` while desktop is `/{...}`. FastAPI
picks the more specific match regardless of order, but we still assert
this behavior in `mobile/tests/backend/test_mount_fallback.py`.

## Non-goals

- No business logic (all data endpoints stay in `backend/csm/api/*`)
- No auth changes (SSH tunnel is the trust boundary)
- No WebSocket handlers (existing `/ws/*` routes are shared as-is)
