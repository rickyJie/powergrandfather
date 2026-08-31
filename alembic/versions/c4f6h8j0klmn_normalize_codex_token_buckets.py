"""normalize Codex token buckets and estimated cost

Codex rollout ``input_tokens`` includes cached-read and cache-write input.
CSM historically copied that inclusive number into ``input_tokens`` and also
stored both cache details, while every Tokens query sums all buckets. This
made Codex totals and estimated cost nearly twice their real values.

Rewrite existing Codex raw events and hourly rollups into CSM's canonical,
disjoint bucket semantics. The application change at the same revision
normalizes newly ingested events at the adapter boundary.

Revision ID: c4f6h8j0klmn
Revises: b3d5f7h9j1kl
Create Date: 2026-08-01
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "c4f6h8j0klmn"
down_revision: str | Sequence[str] | None = "b3d5f7h9j1kl"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Frozen copies of the runtime defaults in csm.modules.token.aggregator.
# Migrations must remain reproducible if application defaults change later.
_DEFAULT_RATES: dict[str, dict[str, float]] = {
    "opus": {"in": 15.0, "cc": 18.75, "cr": 1.5, "out": 75.0},
    "sonnet": {"in": 3.0, "cc": 3.75, "cr": 0.3, "out": 15.0},
    "haiku": {"in": 1.0, "cc": 1.25, "cr": 0.1, "out": 5.0},
    "gpt-4o": {"in": 5.0, "cc": 5.0, "cr": 2.5, "out": 15.0},
    "gpt-4o-mini": {"in": 0.15, "cc": 0.15, "cr": 0.075, "out": 0.6},
    "gpt-4.1": {"in": 2.0, "cc": 2.0, "cr": 1.0, "out": 8.0},
    "o3": {"in": 15.0, "cc": 15.0, "cr": 7.5, "out": 60.0},
    "o3-mini": {"in": 3.0, "cc": 3.0, "cr": 1.5, "out": 12.0},
    "codex": {"in": 3.0, "cc": 3.0, "cr": 1.5, "out": 12.0},
}


def _model_family(model: str | None) -> str:
    name = (model or "").lower()
    if "opus" in name:
        return "opus"
    if "haiku" in name:
        return "haiku"
    if "sonnet" in name:
        return "sonnet"
    if "gpt-4o-mini" in name:
        return "gpt-4o-mini"
    if "gpt-4o" in name:
        return "gpt-4o"
    if "gpt-4.1" in name:
        return "gpt-4.1"
    if "o3-mini" in name:
        return "o3-mini"
    if "o3" in name:
        return "o3"
    if name.startswith(("gpt-", "o-")) or "codex" in name:
        return "codex"
    return "sonnet"


def _rates(bind: Any) -> dict[str, dict[str, float]]:
    rates = {family: values.copy() for family, values in _DEFAULT_RATES.items()}
    rows = bind.execute(sa.text(
        "SELECT model_family, input_per_million, cache_creation_per_million, "
        "cache_read_per_million, output_per_million FROM pricing_config"
    )).mappings()
    for row in rows:
        rates[str(row["model_family"])] = {
            "in": float(row["input_per_million"]),
            "cc": float(row["cache_creation_per_million"]),
            "cr": float(row["cache_read_per_million"]),
            "out": float(row["output_per_million"]),
        }
    return rates


def _cost(
    input_tokens: int,
    cache_creation_tokens: int,
    cache_read_tokens: int,
    output_tokens: int,
    model: str | None,
    rates: dict[str, dict[str, float]],
) -> float:
    price = rates.get(_model_family(model), rates["sonnet"])
    return (
        input_tokens * price["in"]
        + cache_creation_tokens * price["cc"]
        + cache_read_tokens * price["cr"]
        + output_tokens * price["out"]
    ) / 1_000_000.0


def _rewrite_table(bind: Any, table: str, *, to_disjoint: bool) -> None:
    rates = _rates(bind)
    rows: Iterable[sa.RowMapping] = bind.execute(sa.text(
        f"SELECT id, model, input_tokens, cache_creation_tokens, "
        f"cache_read_tokens, output_tokens FROM {table} WHERE agent = 'codex'"
    )).mappings()
    updates: list[dict[str, Any]] = []
    for row in rows:
        current_input = max(0, int(row["input_tokens"] or 0))
        cache_creation = max(0, int(row["cache_creation_tokens"] or 0))
        cache_read = max(0, int(row["cache_read_tokens"] or 0))
        output = max(0, int(row["output_tokens"] or 0))
        if to_disjoint:
            input_tokens = max(0, current_input - cache_creation - cache_read)
        else:
            input_tokens = current_input + cache_creation + cache_read
        updates.append({
            "row_id": row["id"],
            "input_tokens": input_tokens,
            "estimated_cost_usd": _cost(
                input_tokens,
                cache_creation,
                cache_read,
                output,
                row["model"],
                rates,
            ),
        })

    statement = sa.text(
        f"UPDATE {table} SET input_tokens = :input_tokens, "
        "estimated_cost_usd = :estimated_cost_usd WHERE id = :row_id"
    )
    for start in range(0, len(updates), 1000):
        bind.execute(statement, updates[start : start + 1000])


def upgrade() -> None:
    bind = op.get_bind()
    _rewrite_table(bind, "raw_token_event", to_disjoint=True)
    _rewrite_table(bind, "hourly_rollup", to_disjoint=True)


def downgrade() -> None:
    bind = op.get_bind()
    _rewrite_table(bind, "raw_token_event", to_disjoint=False)
    _rewrite_table(bind, "hourly_rollup", to_disjoint=False)
