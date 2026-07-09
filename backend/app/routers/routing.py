"""REST API for Layer 1 real-time dispatch (spec §8)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.routing import engine, store
from app.routing.models import Staff
from app.routing.store import DEMO_TIME_SCALE, sim_minutes_to_real_seconds

router = APIRouter(tags=["routing"])

# Safety-net interval for the SSE loop: even with zero explicit writes,
# the stream still needs to periodically call advance_simulation() itself
# to discover lazy time-based transitions (arrivals, tending completing,
# etc.) — nothing else does this once clients stop polling. In between,
# store._signal_change() wakes the loop immediately on any real change,
# so this interval is a ceiling on latency for time-based transitions,
# not the normal case.
SSE_TICK_SECONDS = 1.0


class RouteEventRequest(BaseModel):
    room: str
    symptom_tags: list[str] = Field(default_factory=list)
    submitted_at: datetime

    @field_validator("submitted_at")
    @classmethod
    def _ensure_naive_utc(cls, v: datetime) -> datetime:
        """Clients may send an offset (e.g. a trailing 'Z'), which Pydantic
        parses into a timezone-aware datetime. Everything internally is
        naive UTC (datetime.utcnow()) — normalize once, here, at the
        boundary, so nothing downstream ever has to reconcile aware vs
        naive. Mixing the two raises TypeError on comparison, which is
        exactly what was crashing /events and /staff-positions."""
        if v.tzinfo is not None:
            v = v.astimezone(timezone.utc).replace(tzinfo=None)
        return v


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
    eta: Optional[float] = None
    fatigue_score_at_assignment: Optional[float] = None




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


class DashboardStateResponse(BaseModel):
    """Atomic snapshot of staff + events computed from a *single*
    advance_simulation() tick, under one lock acquisition, so the two
    can never reflect two different instants of sim state.

    Served two ways:
      - GET /dashboard-state: one-off fetch (kept for any client that
        just wants a single current read, e.g. a debug tool).
      - GET /dashboard-stream: this same payload pushed over SSE the
        instant something changes, instead of a client polling for it.
        This is the primary path now — see dashboard_stream() below.
    """
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
        )
        for e in events
    ]


def _build_dashboard_state() -> DashboardStateResponse:
    """Shared by the one-off endpoint and every tick of the SSE stream —
    one atomic snapshot, one lock acquisition, one `now`."""
    with store.routing_lock():
        now = datetime.utcnow()
        store.advance_simulation(now)
        staff_items = [_staff_to_response(s, now) for s in store.all_staff()]
        event_items = _events_to_items(now)

    return DashboardStateResponse(
        server_time=now,
        time_scale=DEMO_TIME_SCALE,
        staff=staff_items,
        events=event_items,
    )


async def _dashboard_state_event_stream() -> AsyncIterator[str]:
    """SSE generator: pushes a full DashboardStateResponse the instant the
    connection opens, then again every time store._signal_change() fires
    (an explicit write, or a lazy time-based transition discovered by this
    loop's own advance_simulation() call), with SSE_TICK_SECONDS as a
    safety-net ceiling so lazy transitions and connection keep-alives
    never wait longer than that even with zero explicit signals.

    Every tick sends real data (not a bare comment) even when nothing
    material changed, because fatigue_score/hours_in_shift are
    continuously-drifting floats — at real-time pacing that drift IS the
    update, not noise, and doubles as the SSE keep-alive so intermediate
    proxies don't time out an idle-looking connection.
    """
    while True:
        state = _build_dashboard_state()
        yield f"data: {state.model_dump_json()}\n\n"
        await store.wait_for_change(timeout=SSE_TICK_SECONDS)


@router.get("/demo-config", response_model=DemoConfigResponse)
async def demo_config() -> DemoConfigResponse:
    return DemoConfigResponse(time_scale=DEMO_TIME_SCALE)


@router.get("/dashboard-state", response_model=DashboardStateResponse)
async def dashboard_state() -> DashboardStateResponse:
    """One-off atomic read. Prefer /dashboard-stream for anything that
    stays open and wants live updates — this is for a single current
    snapshot."""
    return _build_dashboard_state()


@router.get("/dashboard-stream")
async def dashboard_stream() -> StreamingResponse:
    """Server-Sent Events stream of DashboardStateResponse snapshots.
    Replaces client-side polling: instead of every client independently
    asking "what's true now?" every N seconds, the backend pushes the
    instant something actually changes (bounded by SSE_TICK_SECONDS as a
    ceiling, not a floor). One central loop drives every connected
    client, rather than N clients each running their own timer.

    Headers: no-cache and X-Accel-Buffering:no ask any intermediate
    proxy (Render's included) not to buffer the stream — buffering would
    turn "push the instant it changes" back into "wait for a chunk to
    fill up," defeating the point.
    """
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
        tier=result.tier,
        assigned_staff_id=result.assigned_staff_id,
        eta=result.eta,
        fatigue_score_at_assignment=result.fatigue_score_at_assignment,
        status=result.status,
    )


@router.get("/staff-positions", response_model=list[StaffPositionItem])
async def staff_positions() -> list[StaffPositionItem]:
    now = datetime.utcnow()
    # NOTE: kept for backwards compatibility. /dashboard-stream is the
    # primary path now; this and /events remain for any client that just
    # wants a one-off poll.
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
