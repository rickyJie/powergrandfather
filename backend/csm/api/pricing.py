"""REST endpoints for PricingConfig (per-family token price overrides)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from csm.api._deps import get_db_sessionmaker
from csm.models import PricingConfig
from csm.modules.token.aggregator import RATES, refresh_pricing_overrides
from csm.utils.time import now_utc_naive

router = APIRouter(prefix="/api/pricing", tags=["pricing"])


def _serialize(p: PricingConfig) -> dict[str, Any]:
    return {
        "model_family": p.model_family,
        "input_per_million": p.input_per_million,
        "cache_creation_per_million": p.cache_creation_per_million,
        "cache_read_per_million": p.cache_read_per_million,
        "output_per_million": p.output_per_million,
        "updated_at": p.updated_at.isoformat() + "Z" if p.updated_at else None,
    }


class UpsertPricing(BaseModel):
    model_family: str = Field(..., min_length=1, max_length=50)
    input_per_million: float = Field(..., ge=0)
    cache_creation_per_million: float = Field(..., ge=0)
    cache_read_per_million: float = Field(..., ge=0)
    output_per_million: float = Field(..., ge=0)


@router.get("")
async def list_pricing(sm: async_sessionmaker = Depends(get_db_sessionmaker)):
    async with sm() as db:
        rows = (await db.execute(select(PricingConfig))).scalars().all()
        overrides = [_serialize(r) for r in rows]
    defaults = [
        {
            "model_family": fam,
            "input_per_million": r["in"],
            "cache_creation_per_million": r["cc"],
            "cache_read_per_million": r["cr"],
            "output_per_million": r["out"],
            "source": "default",
        }
        for fam, r in RATES.items()
    ]
    # Overlay: overrides take precedence.
    override_keys = {o["model_family"] for o in overrides}
    merged = [{**d, "source": "default"} for d in defaults if d["model_family"] not in override_keys]
    merged += [{**o, "source": "override"} for o in overrides]
    return {"items": sorted(merged, key=lambda x: x["model_family"])}


@router.put("/{model_family}")
async def upsert(
    model_family: str,
    body: UpsertPricing,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    if body.model_family != model_family:
        raise HTTPException(status_code=400, detail="path model_family must match body")
    async with sm() as db:
        row = await db.get(PricingConfig, model_family)
        if row is None:
            row = PricingConfig(
                model_family=model_family,
                input_per_million=body.input_per_million,
                cache_creation_per_million=body.cache_creation_per_million,
                cache_read_per_million=body.cache_read_per_million,
                output_per_million=body.output_per_million,
                updated_at=now_utc_naive(),
            )
            db.add(row)
        else:
            row.input_per_million = body.input_per_million
            row.cache_creation_per_million = body.cache_creation_per_million
            row.cache_read_per_million = body.cache_read_per_million
            row.output_per_million = body.output_per_million
            row.updated_at = now_utc_naive()
        await db.commit()
        await db.refresh(row)
    # Reload process-local cache immediately so subsequent ingestion uses new rates.
    await refresh_pricing_overrides(sm)
    return _serialize(row)


@router.delete("/{model_family}")
async def delete(
    model_family: str,
    sm: async_sessionmaker = Depends(get_db_sessionmaker),
):
    async with sm() as db:
        row = await db.get(PricingConfig, model_family)
        if row is None:
            raise HTTPException(status_code=404, detail="not found")
        await db.delete(row)
        await db.commit()
    await refresh_pricing_overrides(sm)
    return {"deleted": model_family}
