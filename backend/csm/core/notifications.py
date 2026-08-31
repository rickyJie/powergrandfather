"""In-memory notification value objects mirrored to the Notification ORM model."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from csm.models.notification import NotificationType
from csm.utils.time import now_utc_naive


@dataclass
class NotificationVO:
    type: NotificationType
    title: str
    body: str | None = None
    session_id: str | None = None
    created_at: datetime = field(default_factory=now_utc_naive)
    metadata: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


__all__ = ["NotificationVO"]
