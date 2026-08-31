"""REST endpoints for Budgets."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.api._serialize import iso_utc
from csm.models import Budget
from csm.models.budget import BudgetAction, BudgetPeriod, BudgetScopeType
from csm.modules.token.budget import compute_status

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


def _serialize(b: Budget) -> dict[str, Any]:
    return {
        "id": b.id,
        "name": b.name,
        "enabled": b.enabled,
        "scope_type": b.scope_type.value if hasattr(b.scope_type, "value") else b.scope_type,
        "scope_value": b.scope_value,
        "period": b.period.value if hasattr(b.period, "value") else b.period,
        "token_limit": b.token_limit,
        "cost_limit": b.cost_limit,
        "warn_pct": b.warn_pct,
        "action": b.action.value if hasattr(b.action, "value") else b.action,
        "notify_channel": b.notify_channel or [],
        "cooldown_minutes": b.cooldown_minutes,
        "last_fired_at": iso_utc(b.last_fired_at),
        "last_state": b.last_state,
        "created_at": iso_utc(b.created_at),
        "updated_at": iso_utc(b.updated_at),
    }


class CreateBudget(BaseModel):
    name: str
    scope_type: BudgetScopeType = BudgetScopeType.GLOBAL
    scope_value: str | None = None
    period: BudgetPeriod = BudgetPeriod.DAILY
    token_limit: int | None = Field(default=None, ge=0)
    cost_limit: float | None = Field(default=None, ge=0)
    warn_pct: float = Field(default=80.0, ge=0, le=100)
    action: BudgetAction = BudgetAction.WARN
    notify_channel: list[str] | None = None
    cooldown_minutes: int = Field(default=60, ge=0)
    enabled: bool = True


class PatchBudget(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    scope_value: str | None = None
    period: BudgetPeriod | None = None
    token_limit: int | None = Field(default=None, ge=0)
    cost_limit: float | None = Field(default=None, ge=0)
    warn_pct: float | None = Field(default=None, ge=0, le=100)
    action: BudgetAction | None = None
    notify_channel: list[str] | None = None
    cooldown_minutes: int | None = Field(default=None, ge=0)


@router.get("")
async def list_budgets(
    enabled_only: bool = False,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as db:
        stmt = select(Budget)
        if enabled_only:
            stmt = stmt.where(Budget.enabled.is_(True))
        rows = (await db.execute(stmt)).scalars().all()
        return {"items": [_serialize(b) for b in rows]}


@router.post("")
async def create_budget(
    body: CreateBudget,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    if body.token_limit is None and body.cost_limit is None:
        raise HTTPException(status_code=400, detail="at least one of token_limit / cost_limit required")
    if body.scope_type != BudgetScopeType.GLOBAL and not body.scope_value:
        raise HTTPException(status_code=400, detail=f"scope_value required for scope_type={body.scope_type.value}")
    async with sm() as db:
        b = Budget(
            name=body.name,
            scope_type=body.scope_type,
            scope_value=body.scope_value,
            period=body.period,
            token_limit=body.token_limit,
            cost_limit=body.cost_limit,
            warn_pct=body.warn_pct,
            action=body.action,
            notify_channel=body.notify_channel,
            cooldown_minutes=body.cooldown_minutes,
            enabled=body.enabled,
        )
        db.add(b)
        await db.commit()
        await db.refresh(b)
        return _serialize(b)


@router.patch("/{budget_id}")
async def patch_budget(
    budget_id: str,
    body: PatchBudget,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as db:
        b = await db.get(Budget, budget_id)
        if b is None:
            raise HTTPException(status_code=404, detail="not found")
        for field, value in body.model_dump(exclude_unset=True).items():
            setattr(b, field, value)
        await db.commit()
        await db.refresh(b)
        return _serialize(b)


@router.delete("/{budget_id}")
async def delete_budget(
    budget_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as db:
        b = await db.get(Budget, budget_id)
        if b is None:
            raise HTTPException(status_code=404, detail="not found")
        await db.delete(b)
        await db.commit()
        return {"deleted": budget_id}


@router.get("/status")
async def all_status(
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    """Return status for every enabled budget."""
    async with sm() as db:
        rows = (
            (await db.execute(select(Budget).where(Budget.enabled.is_(True))))
            .scalars().all()
        )
        items = [await compute_status(db, b) for b in rows]
        return {"items": items}


@router.get("/{budget_id}/status")
async def one_status(
    budget_id: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as db:
        b = await db.get(Budget, budget_id)
        if b is None:
            raise HTTPException(status_code=404, detail="not found")
        return await compute_status(db, b)
