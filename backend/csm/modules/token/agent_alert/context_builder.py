"""Rich context blob for agent escalation.

When a rule with `escalate=True` fires, we don't just send "threshold crossed"
— we compute enough diagnostic data for `claude -p` to say things like:

    * Session abc12345 (Bash loops in /home/foo/bar) burned 480K tokens.
    * Opus took 80% of the window; Sonnet-only sessions would have been 3x cheaper.
    * Cache hit ratio dropped to 12% (was 60% yesterday) — check prompt stability.

Data sources (last 5h of `raw_token_event` + `tool_invocation`):
  - Top-N sessions by total tokens (with per-session tool call breakdown).
  - Model split by tokens + cost.
  - Overall cache-hit ratio.
  - Per-minute total-tokens curve for the last 30 minutes (spike vs plateau).

The returned `ContextBlob` has both `to_dict()` (for API / debug) and
`to_markdown()` (for feeding to the LLM).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.models import RawTokenEvent, ToolInvocation
from csm.utils.time import now_utc_naive

_TOP_SESSIONS = 5
_TOP_TOOLS_PER_SESSION = 6
_CURVE_MINUTES = 30
_WINDOW_HOURS = 5.0


@dataclass
class SessionSlice:
    external_session_id: str | None
    project_path: str | None
    model: str | None
    source: str | None
    msg_count: int
    total_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    estimated_cost_usd: float
    tools: list[dict[str, Any]] = field(default_factory=list)  # [{name, count, total_tokens}]


@dataclass
class ContextBlob:
    """Everything the escalation prompt needs, structured."""
    window_hours: float
    window_start: str
    window_end: str
    window_snapshot: dict[str, Any]
    top_sessions: list[SessionSlice]
    model_split: list[dict[str, Any]]         # [{model, total_tokens, cost, share_pct}]
    cache_hit_ratio: float                    # cache_read / (cache_read + input + output)
    minute_curve: list[dict[str, Any]]        # [{minute_utc, total_tokens}]

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": {
                "hours": self.window_hours,
                "start_utc": self.window_start,
                "end_utc": self.window_end,
                "snapshot": self.window_snapshot,
            },
            "top_sessions": [
                {
                    "external_session_id": s.external_session_id,
                    "project_path": s.project_path,
                    "model": s.model,
                    "source": s.source,
                    "msg_count": s.msg_count,
                    "total_tokens": s.total_tokens,
                    "cache_read_tokens": s.cache_read_tokens,
                    "cache_creation_tokens": s.cache_creation_tokens,
                    "estimated_cost_usd": round(s.estimated_cost_usd, 4),
                    "tools": s.tools,
                }
                for s in self.top_sessions
            ],
            "model_split": self.model_split,
            "cache_hit_ratio": round(self.cache_hit_ratio, 3),
            "minute_curve": self.minute_curve,
        }

    def to_markdown(self) -> str:
        lines: list[str] = []
        lines.append("# Token usage context")
        lines.append("")
        lines.append(f"Window: last {self.window_hours}h "
                     f"(`{self.window_start}` → `{self.window_end}`)")
        w = self.window_snapshot
        lines.append(
            f"- Total: **{w.get('total_tokens', 0):,} tokens** "
            f"across {w.get('msg_count', 0):,} messages "
            f"(≈ ${w.get('estimated_cost_usd', 0):.2f})"
        )
        lines.append(
            f"- Cache: read={w.get('cache_read_tokens', 0):,}, "
            f"create={w.get('cache_creation_tokens', 0):,}, "
            f"hit ratio={self.cache_hit_ratio:.1%}"
        )
        lines.append("")

        # Model split
        lines.append("## Model split")
        if not self.model_split:
            lines.append("- (no data)")
        for m in self.model_split:
            lines.append(
                f"- `{m['model'] or '(unknown)'}`: {m['total_tokens']:,} tokens "
                f"({m['share_pct']:.0f}%) · ${m['cost']:.2f}"
            )
        lines.append("")

        # Top sessions + tools
        lines.append(f"## Top {len(self.top_sessions)} sessions by tokens")
        if not self.top_sessions:
            lines.append("- (no sessions)")
        for i, s in enumerate(self.top_sessions, 1):
            sid = (s.external_session_id or "?")[:8]
            lines.append(
                f"### {i}. `{sid}` — {s.total_tokens:,} tokens "
                f"({s.msg_count} msgs, ${s.estimated_cost_usd:.2f})"
            )
            lines.append(f"- Project: `{s.project_path or '?'}`")
            lines.append(f"- Model: `{s.model or '?'}`  ·  Source: `{s.source or '?'}`")
            lines.append(
                f"- Cache read: {s.cache_read_tokens:,}  ·  "
                f"Cache create: {s.cache_creation_tokens:,}"
            )
            if s.tools:
                tool_summary = ", ".join(
                    f"{t['name']}×{t['count']} ({t['total_tokens']:,}t)"
                    for t in s.tools[:_TOP_TOOLS_PER_SESSION]
                )
                lines.append(f"- Top tools: {tool_summary}")
            lines.append("")

        # Minute curve
        lines.append(f"## Minute-level tokens (last {_CURVE_MINUTES} min)")
        if not self.minute_curve:
            lines.append("- (no data)")
        else:
            for pt in self.minute_curve:
                lines.append(f"- `{pt['minute_utc']}`: {pt['total_tokens']:,}")
        return "\n".join(lines)


async def build_context(
    sessionmaker: async_sessionmaker,
    *,
    window_snapshot: dict[str, Any],
    top_n: int = _TOP_SESSIONS,
) -> ContextBlob:
    end = now_utc_naive()
    start = end - timedelta(hours=_WINDOW_HOURS)

    async with sessionmaker() as db:
        total_expr = (
            RawTokenEvent.input_tokens
            + RawTokenEvent.cache_creation_tokens
            + RawTokenEvent.cache_read_tokens
            + RawTokenEvent.output_tokens
        )

        # Top-N sessions
        stmt_sessions = (
            select(
                RawTokenEvent.external_session_id,
                RawTokenEvent.project_path,
                RawTokenEvent.model,
                RawTokenEvent.source,
                func.count(RawTokenEvent.id).label("msg_count"),
                func.coalesce(func.sum(total_expr), 0).label("total"),
                func.coalesce(func.sum(RawTokenEvent.cache_read_tokens), 0).label("cr"),
                func.coalesce(func.sum(RawTokenEvent.cache_creation_tokens), 0).label("cc"),
                func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0).label("cost"),
            )
            .where(RawTokenEvent.ts > start, RawTokenEvent.ts <= end)
            .group_by(
                RawTokenEvent.external_session_id,
                RawTokenEvent.project_path,
                RawTokenEvent.model,
                RawTokenEvent.source,
            )
            .order_by(func.coalesce(func.sum(total_expr), 0).desc())
            .limit(top_n)
        )
        rows = (await db.execute(stmt_sessions)).all()

        top_sessions: list[SessionSlice] = []
        for row in rows:
            sid = row.external_session_id
            tools: list[dict[str, Any]] = []
            if sid:
                tool_stmt = (
                    select(
                        ToolInvocation.tool_name,
                        func.count(ToolInvocation.id).label("count"),
                        func.coalesce(
                            func.sum(
                                ToolInvocation.input_tokens
                                + ToolInvocation.cache_creation_tokens
                                + ToolInvocation.cache_read_tokens
                                + ToolInvocation.output_tokens
                            ),
                            0,
                        ).label("total"),
                    )
                    .where(
                        ToolInvocation.external_session_id == sid,
                        ToolInvocation.ts > start,
                        ToolInvocation.ts <= end,
                    )
                    .group_by(ToolInvocation.tool_name)
                    .order_by(func.coalesce(
                        func.sum(
                            ToolInvocation.input_tokens
                            + ToolInvocation.cache_creation_tokens
                            + ToolInvocation.cache_read_tokens
                            + ToolInvocation.output_tokens
                        ), 0,
                    ).desc())
                    .limit(_TOP_TOOLS_PER_SESSION)
                )
                trows = (await db.execute(tool_stmt)).all()
                tools = [
                    {"name": t.tool_name or "?", "count": int(t.count), "total_tokens": int(t.total)}
                    for t in trows
                ]
            top_sessions.append(SessionSlice(
                external_session_id=sid,
                project_path=row.project_path,
                model=row.model,
                source=row.source,
                msg_count=int(row.msg_count),
                total_tokens=int(row.total),
                cache_read_tokens=int(row.cr),
                cache_creation_tokens=int(row.cc),
                estimated_cost_usd=float(row.cost),
                tools=tools,
            ))

        # Model split
        model_stmt = (
            select(
                RawTokenEvent.model,
                func.coalesce(func.sum(total_expr), 0).label("total"),
                func.coalesce(func.sum(RawTokenEvent.estimated_cost_usd), 0.0).label("cost"),
            )
            .where(RawTokenEvent.ts > start, RawTokenEvent.ts <= end)
            .group_by(RawTokenEvent.model)
            .order_by(func.coalesce(func.sum(total_expr), 0).desc())
        )
        m_rows = (await db.execute(model_stmt)).all()
        grand_total = sum(int(r.total) for r in m_rows) or 1
        model_split = [
            {
                "model": r.model,
                "total_tokens": int(r.total),
                "cost": float(r.cost),
                "share_pct": round(100.0 * int(r.total) / grand_total, 1),
            }
            for r in m_rows
        ]

        # Cache ratio
        cache_read = int(window_snapshot.get("cache_read_tokens", 0) or 0)
        non_cache = (
            int(window_snapshot.get("input_tokens", 0) or 0)
            + int(window_snapshot.get("output_tokens", 0) or 0)
        )
        denom = cache_read + non_cache
        cache_hit_ratio = (cache_read / denom) if denom > 0 else 0.0

        # Minute curve (last 30 min)
        curve_start = end - timedelta(minutes=_CURVE_MINUTES)
        # SQLite-specific: cast unix epoch to minutes, group.
        # Using strftime keeps this portable enough for both SQLite and Postgres.
        minute_col = func.strftime("%Y-%m-%d %H:%M", RawTokenEvent.ts).label("minute_utc")
        curve_stmt = (
            select(
                minute_col,
                func.cast(func.coalesce(func.sum(total_expr), 0), Integer).label("total_tokens"),
            )
            .where(RawTokenEvent.ts > curve_start, RawTokenEvent.ts <= end)
            .group_by(minute_col)
            .order_by(minute_col)
        )
        c_rows = (await db.execute(curve_stmt)).all()
        minute_curve = [
            {"minute_utc": r.minute_utc, "total_tokens": int(r.total_tokens)}
            for r in c_rows
        ]

    return ContextBlob(
        window_hours=_WINDOW_HOURS,
        window_start=window_snapshot.get("start_utc", ""),
        window_end=window_snapshot.get("end_utc", ""),
        window_snapshot=window_snapshot,
        top_sessions=top_sessions,
        model_split=model_split,
        cache_hit_ratio=cache_hit_ratio,
        minute_curve=minute_curve,
    )
