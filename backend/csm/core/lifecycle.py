"""Common lifecycle protocol for lifespan-managed subsystems."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Startable(Protocol):
    """A subsystem with an explicit start/stop lifecycle.

    Even components that don't have real teardown today should implement
    no-op start/stop so the lifespan orchestration is uniform and adding
    real cleanup later (e.g., cache flush, background task cancel) is a
    non-breaking change.
    """

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
