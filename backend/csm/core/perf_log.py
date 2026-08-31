"""Opt-in request-latency logging for diagnosing "why was the console slow".

Enabled with ``CSM_PERF_LOG=1`` (zero overhead when off — the middleware and
route are only wired in ``main.py`` when enabled). Writes to a dedicated
``csm.perf`` logger backed by ``perf.log`` in the repo root, so it never drowns
``csm.log``.

The whole point is **attribution**: the backend can serve ``/api/sessions`` in
15 ms while the browser sees 10 s. To split the blame we correlate two records
by the client-generated ``X-Request-Id``:

    server line : perf req=<id> ... server_ms=15
    client line : perf [client] req=<id> ... total_ms=10200 ttfb_ms=10180 ...

    client.total_ms - server.server_ms  ==  transport(SSH tunnel) + browser queue

So a single ``grep req=<id> perf.log`` tells you whether a slow request was the
backend, the tunnel, or the browser's HTTP/1.1 6-connection-per-host queue.

What's logged:
  - every ``/api`` request's server-side duration (per-request line),
  - a per-path rollup (count / p50 / p95 / max) every 60 s,
  - whatever the frontend POSTs to ``/api/clientperf`` (its own timing +
    failure context), tagged ``[client]``.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from starlette.middleware.base import BaseHTTPMiddleware

_ROLLUP_INTERVAL_SEC = 60.0
_MAX_SAMPLES_PER_PATH = 1024


def perf_enabled() -> bool:
    return os.environ.get("CSM_PERF_LOG") == "1"


# ── dedicated logger → perf.log (set up once, lazily) ───────────────────────
_perf_logger: logging.Logger | None = None


def _logger() -> logging.Logger:
    global _perf_logger
    if _perf_logger is not None:
        return _perf_logger
    lg = logging.getLogger("csm.perf")
    lg.setLevel(logging.INFO)
    lg.propagate = False  # don't echo into the root csm handler / csm.log
    if not lg.handlers:
        try:
            fh = logging.FileHandler(Path.cwd() / "perf.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
            lg.addHandler(fh)
        except OSError:
            lg.addHandler(logging.StreamHandler())
    _perf_logger = lg
    return lg


# ── per-path rollup state (flushed inline every _ROLLUP_INTERVAL_SEC) ────────
_samples: dict[str, list[float]] = {}
_last_flush: float = time.monotonic()


def _normalize(path: str) -> str:
    """Collapse ids so the rollup groups sensibly.

    /api/sessions/3f2a.../changes -> /api/sessions/:id/changes
    """
    parts = path.split("/")
    out: list[str] = []
    for p in parts:
        # crude id detector: long hex/uuid-ish or all-digits segment
        if len(p) >= 12 and all(c in "0123456789abcdef-" for c in p.lower()):
            out.append(":id")
        elif p.isdigit():
            out.append(":id")
        else:
            out.append(p)
    return "/".join(out)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, int(q * (len(sorted_vals) - 1) + 0.5))
    return sorted_vals[idx]


def _maybe_flush_rollup(now: float) -> None:
    global _last_flush, _samples
    if now - _last_flush < _ROLLUP_INTERVAL_SEC:
        return
    _last_flush = now
    snapshot, _samples = _samples, {}
    if not snapshot:
        return
    lg = _logger()
    for key in sorted(snapshot):
        surface, _, norm = key.partition("\t")
        vals = sorted(snapshot[key])
        lg.info(
            "rollup surface=%s path=%s count=%d p50_ms=%.0f p95_ms=%.0f max_ms=%.0f",
            surface, norm, len(vals), _percentile(vals, 0.50),
            _percentile(vals, 0.95), max(vals),
        )


def _record(surface: str, path: str, ms: float) -> None:
    key = f"{surface}\t{_normalize(path)}"
    bucket = _samples.setdefault(key, [])
    if len(bucket) < _MAX_SAMPLES_PER_PATH:
        bucket.append(ms)


class PerfLogMiddleware(BaseHTTPMiddleware):
    """Time every /api request server-side and log it with its X-Request-Id."""

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        req_id = request.headers.get("x-request-id") or "-"
        surface = request.headers.get("x-csm-surface") or "-"
        start = time.perf_counter()
        status = 0
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            ms = (time.perf_counter() - start) * 1000.0
            try:
                _record(surface, path, ms)
                _logger().info(
                    "req=%s surface=%s method=%s status=%d server_ms=%.1f path=%s",
                    req_id, surface, request.method, status, ms, path,
                )
                _maybe_flush_rollup(time.monotonic())
            except Exception:
                pass  # never let logging break the request


router = APIRouter(prefix="/api/clientperf", tags=["perf"])


@router.post("")
async def ingest_client_perf(request: Request) -> dict[str, int]:
    """Sink for frontend perf entries so client evidence lands in perf.log too.

    Accepts a JSON array (or single object) of arbitrary timing/failure
    records. Each is logged tagged ``[client]`` — correlate against the server
    line by its ``req`` field.

    When perf logging is disabled this route still exists (so the frontend
    beacon gets a 200 instead of a 405), but it's a pure no-op: we don't parse
    the body and we never open perf.log.
    """
    if not perf_enabled():
        return {"ingested": 0}
    try:
        body: Any = await request.json()
    except Exception:
        return {"ingested": 0}
    entries = body if isinstance(body, list) else [body]
    lg = _logger()
    n = 0
    for e in entries:
        if not isinstance(e, dict):
            continue
        req = e.get("req") or e.get("reqId") or "-"
        lg.info("[client] req=%s %s", req, json.dumps(e, default=str, ensure_ascii=False))
        n += 1
    return {"ingested": n}


__all__ = ["PerfLogMiddleware", "perf_enabled", "router"]
