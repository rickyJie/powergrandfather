# ADR 0002 — Single-process backend monolith

**Date**: 2026-06-20
**Status**: Accepted
**Phase**: design (pre-P0)

## Context

The backend has six logical modules (M1-M6) that could be split into separate processes communicating over IPC. Examples of microservice-flavored designs would put:
- Session Manager as its own daemon (it owns PTY subprocess fan-out)
- Event Stream as a Pub/Sub broker (Redis or NATS)
- Token aggregator as a separate worker
- Reverse proxy as nginx

## Decision

**v1 is a single FastAPI process with one SQLite database.** All modules live in one Python process and share an asyncio event loop.

## Why

1. **Scale is tiny.** Target deployment is one user, ~15 concurrent sessions, ~10 ports, ~10 workflows. A single process handles this with cycles to spare.
2. **No remote consumers.** All state and all consumers live on the same machine — there is no network boundary that justifies IPC.
3. **Less operational surface.** One uvicorn process to start/stop, one SQLite file to back up, one log to read. Microservices would multiply observability burden without business benefit.
4. **Async-friendly modules.** Event Stream, Token aggregator, Notification Bus, Port scanner all do interleaved I/O. asyncio in one process is the most natural model.
5. **Trivial cross-module wiring.** Event Stream is just `subscribe(callback)`; no serialization, no broker semantics, no at-least-once vs exactly-once headaches.
6. **SQLite is enough.** 12 tables, sub-MB DB size in normal use. SQLAlchemy async + WAL mode handles concurrency for our scale.

## Consequences

- (-) Can't scale by adding more backend instances. Acceptable: single user.
- (-) A bug in one module can take down the whole process. Mitigation: all loops wrap user code in `try/except`.
- (-) No per-module observability (Prometheus exporters etc.). Mitigation: logging + on-screen dashboards.
- (+) ≤ 500 LoC of "infrastructure code" (db.py, main.py, event_stream.py); everything else is business logic.
- (+) Single integration test process to debug.
- (+) Deployment = `git clone + pip install -e . + alembic upgrade + uvicorn`.

## Future direction

If we ever need:
- **Multi-user / team**: introduce auth, switch to Postgres, split Session Manager into its own daemon (PTY ownership is the natural boundary)
- **Cross-machine**: same as above but with Event Stream → Redis pub/sub
- **Hot reload of UI without backend restart**: serve frontend from CDN + run backend as plain API

None of these are on the v1 roadmap.
