"""REST API for Layer 1 real-time dispatch (spec §8)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.routing import engine, store
from app.routing.models import Staff

router = APIRouter(tags=["routing"])


class RouteEventRequest(BaseModel):
    room: str
    symptom_tags: list[str] = Field(default_factory=list)
    submitted_at: datetime


class RouteEventResponse(BaseModel):
    event_id: str
    tier: int
    assigned_staff_id: Optional[str]
    eta: Optional[float]
    fatigue_score_at_assignment: Optional[float]
    status: Literal["pending", "assigned", "resolved", "escalated"]


class StaffPositionResponse(BaseModel):
    room: str


class StaffPositionItem(BaseModel):
    staff_id: str
    role: str
    qualification_level: str
    shift_start: datetime
    current_position: StaffPositionResponse
    status: str
    current_event_id: Optional[str]
    hours_in_shift: float
    fatigue_score: float


class ClearAssignmentRequest(BaseModel):
    event_id: str


class ClearAssignmentResponse(BaseModel):
    event_id: str
    status: Literal["resolved"]
    reassigned_pending: Optional[RouteEventResponse] = None


def _staff_to_response(staff: Staff, now: datetime) -> StaffPositionItem:
    from app.routing.fatigue import compute_fatigue, hours_in_shift

    return StaffPositionItem(
        staff_id=staff.staff_id,
        role=staff.role,
        qualification_level=staff.qualification_level,
        shift_start=staff.shift_start,
        current_position=StaffPositionResponse(room=staff.current_position.room),
        status=staff.status,
        current_event_id=staff.current_event_id,
        hours_in_shift=hours_in_shift(staff.shift_start, now),
        fatigue_score=compute_fatigue(staff.shift_start, staff.task_history, now),
    )


@router.post("/route-event", response_model=RouteEventResponse)
async def route_event(payload: RouteEventRequest) -> RouteEventResponse:
    result = engine.route_event_sequential(
        room=payload.room,
        symptom_tags=payload.symptom_tags,
        submitted_at=payload.submitted_at,
    )
    return RouteEventResponse(
        event_id=result.event_id,
        tier=result.tier,
        assigned_staff_id=result.assigned_staff_id,
        eta=result.eta,
        fatigue_score_at_assignment=result.fatigue_score_at_assignment,
        status=result.status,
    )


@router.get("/staff-positions", response_model=list[StaffPositionItem])
async def staff_positions() -> list[StaffPositionItem]:
    now = datetime.utcnow()
    return [_staff_to_response(s, now) for s in store.all_staff()]


@router.post("/clear-assignment", response_model=ClearAssignmentResponse)
async def clear_assignment(payload: ClearAssignmentRequest) -> ClearAssignmentResponse:
    reassigned = engine.clear_assignment(payload.event_id)
    event = store.get_event(payload.event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    reassigned_response = None
    if reassigned is not None:
        reassigned_response = RouteEventResponse(
            event_id=reassigned.event_id,
            tier=reassigned.tier,
            assigned_staff_id=reassigned.assigned_staff_id,
            eta=reassigned.eta,
            fatigue_score_at_assignment=reassigned.fatigue_score_at_assignment,
            status=reassigned.status,
        )

    return ClearAssignmentResponse(
        event_id=payload.event_id,
        status="resolved",
        reassigned_pending=reassigned_response,
    )


@router.post("/reset-simulation")
async def reset_simulation() -> dict[str, str]:
    store.reset_simulation()
    return {"status": "reset"}
