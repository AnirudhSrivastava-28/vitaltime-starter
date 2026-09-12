"""REST API for Layer 1 real-time dispatch (spec §8)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.routing import engine, store
from app.routing.models import Staff
from app.routing.store import DEMO_TIME_SCALE, sim_minutes_to_real_seconds

router = APIRouter(tags=["routing"])

SSE_TICK_SECONDS = 1.0


class RouteEventRequest(BaseModel):
    room: str
    symptom_tags: list[str] = Field(default_factory=list)
    submitted_at: datetime

    @field_validator("submitted_at")
    @classmethod
    def _ensure_naive_utc(cls, v: datetime) -> datetime:
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


class RouteEventResponse(BaseModel):
    event_id: str
    room: str
    tier: int
    assigned_staff_id: Optional[str]
    eta: Optional[float]
    fatigue_score_at_assignment: Optional[float]
    status: Literal["pending", "assigned", "resolved", "escalated"]
    # Best-effort — only ever non-null while status == "pending" and every
    # qualified candidate is currently uninterruptible-busy. Not a
    # reservation; see engine._predict_next_available_staff for the
    # exact semantics. Lets the client show "next up: RN-001, ~90s"
    # instead of a bare "pending" with no further information.
    predicted_next_staff_id: Optional[str] = None


class StaffPositionResponse(BaseModel):
    room: str


class TransitPlanResponse(BaseModel):
    path: list[str]
    hop_times: list[float]
    hop_durations_seconds: list[float]
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
    eta: Optional[float] = None
    fatigue_score_at_assignment: Optional[float] = None
    predicted_next_staff_id: Optional[str] = None


class ClearAssignmentRequest(BaseModel):
    event_id: str


class ClearAssignmentResponse(BaseModel):
    event_id: str
    status: Literal["resolved"]
    reassigned_pending: Optional[RouteEventResponse] = None


class DemoConfigResponse(BaseModel):
    time_scale: float
    poll_interval_ms: int = 1500


class DashboardStateResponse(BaseModel):
    server_time: datetime
    time_scale: float
    staff: list[StaffPositionItem]
    events: list[EventItem]


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


def _events_to_items(now: datetime) -> list[EventItem]:
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
            eta=e.eta,
            fatigue_score_at_assignment=e.fatigue_score_at_assignment,
            predicted_next_staff_id=e.predicted_next_staff_id,
        )
        for e in events
    ]


def _build_dashboard_state() -> DashboardStateResponse:
    with store.routing_lock():
        now = datetime.utcnow()
        engine.tick(now)
        staff_items = [_staff_to_response(s, now) for s in store.all_staff()]
        event_items = _events_to_items(now)

    return DashboardStateResponse(
        server_time=now,
        time_scale=DEMO_TIME_SCALE,
        staff=staff_items,
        events=event_items,
    )


async def _dashboard_state_event_stream() -> AsyncIterator[str]:
    while True:
        state = _build_dashboard_state()
        yield f"data: {state.model_dump_json()}\n\n"
        await store.wait_for_change(timeout=SSE_TICK_SECONDS)


@router.get("/demo-config", response_model=DemoConfigResponse)
async def demo_config() -> DemoConfigResponse:
    return DemoConfigResponse(time_scale=DEMO_TIME_SCALE)


@router.get("/dashboard-state", response_model=DashboardStateResponse)
async def dashboard_state(response: Response) -> DashboardStateResponse:
    # No caching layer should ever serve a stale snapshot for this
    # endpoint — see the dashboard.html patch notes for why this
    # mattered even on a single instance (uncached-by-default is not the
    # same as guaranteed-not-cached by some intermediate proxy).
    response.headers["Cache-Control"] = "no-store"
    return _build_dashboard_state()


@router.get("/dashboard-stream")
async def dashboard_stream() -> StreamingResponse:
    return StreamingResponse(
        _dashboard_state_event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
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
        room=payload.room,
        tier=result.tier,
        assigned_staff_id=result.assigned_staff_id,
        eta=result.eta,
        fatigue_score_at_assignment=result.fatigue_score_at_assignment,
        status=result.status,
        predicted_next_staff_id=result.predicted_next_staff_id,
    )


@router.get("/staff-positions", response_model=list[StaffPositionItem])
async def staff_positions() -> list[StaffPositionItem]:
    now = datetime.utcnow()
    return [_staff_to_response(s, now) for s in store.all_staff()]


@router.get("/events", response_model=list[EventItem])
async def list_events() -> list[EventItem]:
    now = datetime.utcnow()
    return _events_to_items(now)


@router.post("/clear-assignment", response_model=ClearAssignmentResponse)
async def clear_assignment(payload: ClearAssignmentRequest) -> ClearAssignmentResponse:
    reassigned = engine.clear_assignment(payload.event_id)
    event = store.get_event(payload.event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="Event not found")

    reassigned_response = None
    if reassigned is not None:
        reassigned_event = store.get_event(reassigned.event_id)
        event_room = reassigned_event.room if reassigned_event is not None else ""
        reassigned_response = RouteEventResponse(
            event_id=reassigned.event_id,
            room=event_room,
            tier=reassigned.tier,
            assigned_staff_id=reassigned.assigned_staff_id,
            eta=reassigned.eta,
            fatigue_score_at_assignment=reassigned.fatigue_score_at_assignment,
            status=reassigned.status,
            predicted_next_staff_id=reassigned.predicted_next_staff_id,
        )

    return ClearAssignmentResponse(
        event_id=payload.event_id,
        status="resolved",
        reassigned_pending=reassigned_response,
    )


@router.post("/reset-simulation")
async def reset_simulation() -> dict[str, str]:
    engine.reset_simulation()
    return {"status": "reset"}
