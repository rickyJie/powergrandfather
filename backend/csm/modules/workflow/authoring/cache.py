"""In-memory TTL cache for two-round workflow authoring.

Round 1 (`POST /api/workflows/clarify`) stashes the requirement +
questions under a random `clarification_id`. Round 2
(`POST /api/workflows/generate`) reads it back so the generate prompt
can splice the user's answers to those questions.

TTL: 5 minutes. If the user idles longer, the id disappears and generate
falls back to the one-shot (no clarifications) path — the same behavior
as if clarify had never been called.

The cache is attached to `app.state.workflow_clarify_cache` in lifespan
(per project convention — no module-level singletons for new subsystems).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any

DEFAULT_TTL_SEC = 300


@dataclass
class ClarificationEntry:
    id: str
    requirement: str
    workflow_name: str | None
    repo_path: str
    questions: list[dict[str, Any]]
    stage_preview: str
    # Agent-proposed stage skeleton [{name, kind, purpose}]. Frontend
    # renders this in the clarify screen so the user can adjust before
    # generation locks it in. Empty list = agent didn't propose stages
    # (legacy / degraded path).
    stages: list[dict[str, Any]]
    created_at: float


class ClarificationCache:
    """Small thread-safe TTL map.

    Not persistent across backend restarts — that's fine: if the user is
    mid-clarify when we restart, they just re-submit the form.
    """

    def __init__(self, ttl_sec: int = DEFAULT_TTL_SEC) -> None:
        self._ttl = ttl_sec
        self._store: dict[str, ClarificationEntry] = {}
        self._lock = Lock()

    def _sweep_locked(self) -> None:
        now = time.time()
        expired = [
            k for k, v in self._store.items()
            if now - v.created_at > self._ttl
        ]
        for k in expired:
            self._store.pop(k, None)

    def put(
        self,
        *,
        requirement: str,
        workflow_name: str | None,
        repo_path: str,
        questions: list[dict[str, Any]],
        stage_preview: str,
        stages: list[dict[str, Any]] | None = None,
    ) -> str:
        with self._lock:
            self._sweep_locked()
            cid = uuid.uuid4().hex
            self._store[cid] = ClarificationEntry(
                id=cid,
                requirement=requirement,
                workflow_name=workflow_name,
                repo_path=repo_path,
                questions=list(questions),
                stage_preview=stage_preview,
                stages=list(stages) if stages else [],
                created_at=time.time(),
            )
            return cid

    def get(self, cid: str) -> ClarificationEntry | None:
        with self._lock:
            self._sweep_locked()
            return self._store.get(cid)

    def pop(self, cid: str) -> ClarificationEntry | None:
        with self._lock:
            self._sweep_locked()
            return self._store.pop(cid, None)

    def size(self) -> int:
        with self._lock:
            self._sweep_locked()
            return len(self._store)

    async def start(self) -> None:
        """No-op — Startable protocol conformance."""
        return None

    async def stop(self) -> None:
        """No-op — Startable protocol conformance."""
        return None
