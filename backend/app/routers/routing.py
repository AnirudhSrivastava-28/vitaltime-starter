"""REST API for Layer 1 real-time dispatch (spec §8)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.routing import engine, store
from app.routing.models import Staff
from app.routing.store import DEMO_TIME_SCALE, sim_minutes_to_real_seconds

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


class TransitPlanResponse(BaseModel):
    path: list[str]
    hop_times: list[float]              # cumulative SIM minutes (for backwards compat)
    hop_durations_seconds: list[float]  # cumulative REAL wall-clock seconds (client uses these)
    departure_time: datetime
    arrival_time: datetime
    mode: Literal["outbound", "return"]


class StaffPositionItem(BaseModel):
    staff_id: str
    role: str
    qualification_level: str
    shift_start: datetime
    current_position: StaffPositionResponse
    home_room: str
    status: str
    current_event_id: Optional[str]
    hours_in_shift: float
    fatigue_score: float
    transit: Optional[TransitPlanResponse] = None


class EventItem(BaseModel):
    event_id: str
    room: str
    tier: int
    symptom_tags: list[str]
    status: Literal["pending", "assigned", "resolved", "escalated"]
    assigned_staff_id: Optional[str]
    submitted_at: datetime
    tending_until: Optional[datetime] = None


class ClearAssignmentRequest(BaseModel):
    event_id: str


class ClearAssignmentResponse(BaseModel):
    event_id: str
    status: Literal["resolved"]
    reassigned_pending: Optional[RouteEventResponse] = None


class DemoConfigResponse(BaseModel):
    """Timing metadata the client uses to interpret timestamps.
    Exposing this makes the animation resilient to future scale changes —
    the frontend never hardcodes the multiplier."""
    time_scale: float
    poll_interval_ms: int = 1500


def _staff_to_response(staff: Staff, now: datetime) -> StaffPositionItem:
    from app.routing.fatigue import compute_fatigue, hours_in_shift

    transit_resp = None
    if staff.transit is not None:
        total_sim_minutes = staff.transit.hop_times[-1]
        total_real_seconds = sim_minutes_to_real_seconds(total_sim_minutes)
        arrival = staff.transit.departure_time + timedelta(seconds=total_real_seconds)
        transit_resp = TransitPlanResponse(
            path=staff.transit.path,
            hop_times=staff.transit.hop_times,
            hop_durations_seconds=[
                sim_minutes_to_real_seconds(t) for t in staff.transit.hop_times
            ],
            departure_time=staff.transit.departure_time,
            arrival_time=arrival,
            mode=staff.transit.mode,
        )

    return StaffPositionItem(
        staff_id=staff.staff_id,
        role=staff.role,
        qualification_level=staff.qualification_level,
        shift_start=staff.shift_start,
        current_position=StaffPositionResponse(room=staff.current_position.room),
        home_room=staff.home_room,
        status=staff.status,
        current_event_id=staff.current_event_id,
        hours_in_shift=hours_in_shift(staff.shift_start, now),
        fatigue_score=compute_fatigue(staff.shift_start, staff.task_history, now),
        transit=transit_resp,
    )


@router.get("/demo-config", response_model=DemoConfigResponse)
async def demo_config() -> DemoConfigResponse:
    return DemoConfigResponse(time_scale=DEMO_TIME_SCALE)


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
    # store.all_staff() advances the sim internally, so the snapshot we
    # return is guaranteed to reflect any transits/tending/returns that
    # should have completed by `now`.
    return [_staff_to_response(s, now) for s in store.all_staff()]


@router.get("/events", response_model=list[EventItem])
async def list_events() -> list[EventItem]:
    events = sorted(store.all_events(), key=lambda e: e.submitted_at, reverse=True)
    return [
        EventItem(
            event_id=e.event_id,
            room=e.room,
            tier=e.tier,
            symptom_tags=e.symptom_tags,
            status=e.status,
            assigned_staff_id=e.assigned_staff_id,
            submitted_at=e.submitted_at,
            tending_until=e.tending_until,
        )
        for e in events
    ]


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
