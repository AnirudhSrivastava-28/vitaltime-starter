"""Batch comparison API for VitalTime and modeled call-light dispatch."""

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.comparison.simulation import DEFAULT_ASSUMPTIONS, ComparisonConfig, run_comparison

router = APIRouter(tags=["comparison"])


class ComparisonRequest(BaseModel):
    trials: int = Field(default=20, ge=1, le=200)
    seed: int = 20261116
    events_per_day: int = Field(default=300, ge=1, le=1000)
    shift_structure: Literal["standard", "staggered"] = "standard"
    call_light_assumptions: dict[str, float] = Field(
        default_factory=lambda: dict(DEFAULT_ASSUMPTIONS)
    )
    degenerate_call_light: bool = False


@router.post("/comparison/batch")
async def comparison_batch(payload: ComparisonRequest) -> dict:
    try:
        return run_comparison(
            ComparisonConfig(
                trials=payload.trials,
                seed=payload.seed,
                events_per_day=payload.events_per_day,
                shift_structure=payload.shift_structure,
                assumptions=payload.call_light_assumptions,
                degenerate_call_light=payload.degenerate_call_light,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
