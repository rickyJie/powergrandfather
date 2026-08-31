"""Backend-side patch for mounting the mobile SPA under /m/ without touching the main FastAPI app source.

Typical consumers:

- ``mobile/scripts/start_with_mobile.sh`` runs
  ``uvicorn --factory mobile.backend_patch:_factory ...``, which triggers
  ``_factory`` below — import ``csm.main:app`` first, then attach the
  mobile mount, then hand the app to uvicorn.
- Test code (``mobile/tests/backend/*``) calls ``register(app)`` directly
  against a freshly built app.
"""

from fastapi import FastAPI

from mobile.backend_patch.mount import dist_dir, register

__all__ = ["register", "dist_dir", "_factory"]


def _factory() -> FastAPI:
    """uvicorn --factory entrypoint used by start_with_mobile.sh."""
    from csm.main import app

    register(app)
    return app
