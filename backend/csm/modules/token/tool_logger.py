"""ToolInvocationLogger — persist TOOL_INVOKED events for the Tools panel.

Mirrors TokenAggregator's attribution lookup so we can group tool counts by
source (interactive / auto / subagent / manual_external) the same way.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.core.event_stream import EventStream
from csm.core.events import Event, EventType
from csm.models import Session, ToolInvocation
from csm.modules.token.aggregator import estimate_cost

log = logging.getLogger(__name__)


class ToolInvocationLogger:
    def __init__(self, sessionmaker: async_sessionmaker, event_stream: EventStream):
        self._sm = sessionmaker
        self._es = event_stream
        self._sub_id: str | None = None

    async def start(self) -> None:
        self._sub_id = self._es.subscribe([EventType.TOOL_INVOKED], self._on_tool)

    async def stop(self) -> None:
        if self._sub_id:
            self._es.unsubscribe(self._sub_id)
            self._sub_id = None

    async def _on_tool(self, event: Event) -> None:
        payload = event.payload or {}
        name = payload.get("name")
        if not name:
            return
        is_subagent = "/subagents/" in (payload.get("jsonl_path") or "")
        command_type = "subagent" if is_subagent else "direct"
        in_t = int(payload.get("input_tokens", 0) or 0)
        cc = int(payload.get("cache_creation_input_tokens", 0) or 0)
        cr = int(payload.get("cache_read_input_tokens", 0) or 0)
        out = int(payload.get("output_tokens", 0) or 0)
        model = payload.get("model")
        cost = estimate_cost(in_t, cc, cr, out, model)
        async with self._sm() as db:
            source: str | None = "unknown"
            csm_session_id: str | None = None
            if event.session_id:
                sess = (await db.execute(
                    select(Session).where(
                        Session.external_session_id == event.session_id,
                        Session.superseded_by.is_(None),
                    ).limit(1)
                )).scalar_one_or_none()
                if sess is None:
                    source = "manual_external"
                else:
                    source = sess.type.value if hasattr(sess.type, "value") else str(sess.type)
                    csm_session_id = sess.id
            row = ToolInvocation(
                ts=_to_naive(event.ts),
                tool_name=str(name)[:100],
                external_session_id=event.session_id,
                project_path=event.project_path,
                csm_session_id=csm_session_id,
                source=source,
                command_type=command_type,
                input_tokens=in_t,
                cache_creation_tokens=cc,
                cache_read_tokens=cr,
                output_tokens=out,
                estimated_cost_usd=cost,
            )
            db.add(row)
            await db.commit()


def _to_naive(ts: datetime) -> datetime:
    if ts.tzinfo is not None:
        return ts.astimezone(UTC).replace(tzinfo=None)
    return ts
