"""In-memory staff/event state for v1 routing (spec §5, §9).

Adds an authoritative simulation lifecycle: assigned events auto-arrive,
auto-tend, auto-resolve, and staff auto-return home. All state transitions
are driven by wall-clock elapsed time against DEMO_TIME_SCALE, so the
same clock the client uses for animation is the same clock the backend
uses for state changes — client and backend can never disagree about
which phase of the lifecycle a staff member is in.
"""

from __future__ import annotations

import os
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.routing import eta
from app.routing.constants import ROLE_QUALIFICATION
from app.routing.fatigue import prune_task_history
from app.routing.models import Event, Staff, StaffPosition, TaskHistoryEntry, TransitPlan


def _naive_utc(dt: datetime) -> datetime:
    """Normalize any datetime to naive UTC. Everything internal to this
    module compares against datetime.utcnow() (naive); an aware datetime
    slipping in anywhere (e.g. a client-supplied timestamp with a 'Z' or
    offset that a caller forgot to normalize at the API boundary) raises
    TypeError the instant it's compared. Defense-in-depth: normalize here
    too, at every point an externally-supplied `now`/timestamp enters the
    store, not just at the HTTP boundary."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

# ---- demo timing controls ---------------------------------------------------

# Wall-clock acceleration. 1.0 = real time — a 2-minute ETA takes 2 real
# minutes, a 4-minute tending window takes 4 real minutes. This used to
# default to 15x for a fast-moving demo reel, but at that speed an entire
# assigned -> tending -> resolved lifecycle finished in ~10-20 real
# seconds: faster than the dashboard's 1.5s poll cadence could sample
# cleanly (events appeared to pop in and out of the feed as polls skipped
# over whole phases) and faster than the eye can track motion between
# rooms (staff appeared to "fly" across multiple waypoints at once).
# RESPONSE_WINDOWS and TENDING_MINUTES_BY_TIER below are already defined
# in real minutes, so scale=1.0 makes them mean exactly what they say.
# Override via env var if a sped-up demo reel is ever wanted again.
DEMO_TIME_SCALE = float(os.environ.get("VITALTIME_TIME_SCALE", "1.0"))

# How long an event holds a staff member at the room after arrival.
TENDING_MINUTES_BY_TIER: dict[int, float] = {
    1: 4.0,  # emergency: intense but focused
    2: 3.0,  # validation: assess, possibly de-escalate
    3: 2.0,  # normal:    quick routine task
}


def sim_minutes_to_real_seconds(sim_minutes: float) -> float:
    """Convert unscaled sim-minutes into real-wall-clock seconds."""
    return (sim_minutes * 60.0) / DEMO_TIME_SCALE


def real_seconds_to_sim_minutes(real_seconds: float) -> float:
    return (real_seconds / 60.0) * DEMO_TIME_SCALE


def sim_timedelta(sim_minutes: float) -> timedelta:
    """A real-wall-clock timedelta equal to the given sim-minutes at the
    demo's accelerated pace."""
    return timedelta(seconds=sim_minutes_to_real_seconds(sim_minutes))


# ---- store ------------------------------------------------------------------

_lock = threading.RLock()
_staff: dict[str, Staff] = {}
_events: dict[str, Event] = {}
_pending_ids: list[str] = []

# Bounds for the randomized seed scenario — see _seed_staff.
_SEED_SHIFT_HOURS_RANGE = (0.25, 7.5)          # within a plausible 8h shift
_SEED_TASK_COUNT_CHOICES = [0, 1, 2, 3]
_SEED_TASK_COUNT_WEIGHTS = [40, 30, 20, 10]     # most staff start light
_SEED_TASK_AGE_MINUTES_RANGE = (5.0, 200.0)     # stays under the 4h prune window


def _seed_staff(now: datetime) -> None:
    """Demo staff roster for simulation/testing. Each staff member's initial
    room is also their home_room — where they return to after events resolve.

    Both fatigue inputs are randomized on every call (i.e. every cold start
    and every /reset-simulation), rather than fixed:
      - shift-elapsed hours (drives elapsed-time fatigue)
      - a small random number of recent completed tasks (drives workload
        fatigue)

    A flat, identical baseline for every staff member on every reset meant
    the tiered scoring (4:1 ETA:fatigue for Validation, 1:4 for Normal)
    always made the same "who's freshest" call — there was no way to reset
    into a genuinely different staffing-fatigue scenario to compare routing
    behavior against. Randomizing gives a new, plausible mix (some staff
    nearly fresh, some deep into their shift, a few carrying recent task
    load) on every reset.
    """
    roster = [
        ("RN-001", "RN", "NS"),
        ("RN-002", "RN", "108"),
        ("LPN-001", "LPN", "105"),
        ("LPN-002", "LPN", "102"),
        ("CNA-001", "CNA", "101"),
        ("CNA-002", "CNA", "106"),
        ("CNA-003", "CNA", "110"),
    ]
    for staff_id, role, room in roster:
        shift_hours = random.uniform(*_SEED_SHIFT_HOURS_RANGE)
        shift_start = now - timedelta(hours=shift_hours)

        staff = Staff(
            staff_id=staff_id,
            role=role,  # type: ignore[arg-type]
            qualification_level=ROLE_QUALIFICATION[role],  # type: ignore[index]
            shift_start=shift_start,
            current_position=StaffPosition(room=room),
            home_room=room,
        )

        task_count = random.choices(_SEED_TASK_COUNT_CHOICES, weights=_SEED_TASK_COUNT_WEIGHTS)[0]
        for _ in range(task_count):
            minutes_ago = random.uniform(*_SEED_TASK_AGE_MINUTES_RANGE)
            staff.task_history.append(
                TaskHistoryEntry(
                    event_id=f"seed-{staff_id}-{uuid.uuid4().hex[:8]}",
                    completed_at=now - timedelta(minutes=minutes_ago),
                )
            )

        _staff[staff_id] = staff


def _ensure_seeded() -> None:
    if not _staff:
        _seed_staff(datetime.utcnow())


def reset_simulation() -> None:
    with _lock:
        _staff.clear()
        _events.clear()
        _pending_ids.clear()
        _seed_staff(datetime.utcnow())


# ---- lifecycle simulation ---------------------------------------------------

def _advance_single_staff(staff: Staff, now: datetime) -> None:
    """Fire any state transitions that should have happened by `now`.

    Three transitions per staff member per tick:
      (1) Outbound arrival → tending starts (tending_until is set)
      (2) Tending complete → event resolves; return transit begins
      (3) Return arrival   → transit cleared; staff idle at home

    Each transition is idempotent — calling with the same `now` twice is
    safe. A manual /clear-assignment before (2) short-circuits (2)+(3).
    """
    # (1) Outbound arrival
    if (
        staff.status == "busy"
        and staff.transit is not None
        and staff.transit.mode == "outbound"
        and staff.current_event_id is not None
    ):
        arrival = staff.transit.departure_time + sim_timedelta(staff.transit.hop_times[-1])
        if now >= arrival:
            event = _events.get(staff.current_event_id)
            if event and event.status == "assigned" and event.tending_until is None:
                tending_minutes = TENDING_MINUTES_BY_TIER.get(event.tier, 3.0)
                event.tending_until = arrival + sim_timedelta(tending_minutes)

    # (2) Tending complete → resolve
    if (
        staff.status == "busy"
        and staff.current_event_id is not None
    ):
        event = _events.get(staff.current_event_id)
        if (
            event is not None
            and event.status == "assigned"
            and event.tending_until is not None
            and now >= event.tending_until
        ):
            event.status = "resolved"
            resolved_at = event.tending_until
            event.assigned_staff_id = None

            staff.task_history.append(
                TaskHistoryEntry(event_id=event.event_id, completed_at=resolved_at)
            )
            staff.task_history = prune_task_history(staff.task_history, now)
            staff.status = "available"
            staff.current_event_id = None

            # Start return transit from the event room back to home.
            return_path, return_hops = eta.shortest_path(event.room, staff.home_room)
            if len(return_path) > 1:
                staff.transit = TransitPlan(
                    path=return_path,
                    hop_times=return_hops,
                    departure_time=resolved_at,
                    mode="return",
                )
                staff.current_position = StaffPosition(room=staff.home_room)
            else:
                # Already at home (edge case).
                staff.transit = None
                staff.current_position = StaffPosition(room=staff.home_room)

    # (3) Return arrival → idle
    if (
        staff.status == "available"
        and staff.transit is not None
        and staff.transit.mode == "return"
    ):
        return_arrival = staff.transit.departure_time + sim_timedelta(staff.transit.hop_times[-1])
        if now >= return_arrival:
            staff.transit = None


def advance_simulation(now: Optional[datetime] = None) -> None:
    """Advance every staff member's lifecycle up to `now`.

    Called at the top of every read path (all_staff, all_events) so the
    state a client sees is always current — no background threads needed,
    no race between reader and writer, no dependency on how often clients
    poll. If nothing polls for 10 seconds, the next read still resolves
    all transitions that should have happened during those 10 seconds.
    """
    now = _naive_utc(now or datetime.utcnow())
    with _lock:
        for staff in _staff.values():
            _advance_single_staff(staff, now)


# ---- read paths (advance before returning) ----------------------------------

def all_staff() -> list[Staff]:
    _ensure_seeded()
    advance_simulation()
    with _lock:
        return list(_staff.values())


def all_events() -> list[Event]:
    _ensure_seeded()
    advance_simulation()
    with _lock:
        return list(_events.values())


def get_event(event_id: str) -> Optional[Event]:
    with _lock:
        return _events.get(event_id)


def get_staff(staff_id: str) -> Optional[Staff]:
    _ensure_seeded()
    with _lock:
        return _staff.get(staff_id)


# ---- writes -----------------------------------------------------------------

def create_event(
    *,
    room: str,
    tier: int,
    symptom_tags: list[str],
    submitted_at: datetime,
    escalated_from: Optional[str] = None,
) -> Event:
    _ensure_seeded()
    submitted_at = _naive_utc(submitted_at)
    event = Event(
        event_id=str(uuid.uuid4()),
        room=room,
        tier=tier,
        symptom_tags=symptom_tags,
        submitted_at=submitted_at,
        escalated_from=escalated_from,
    )
    with _lock:
        _events[event.event_id] = event
    return event


def enqueue_pending(event_id: str) -> None:
    with _lock:
        if event_id not in _pending_ids:
            _pending_ids.append(event_id)


def pop_pending_queue() -> list[str]:
    with _lock:
        ids = list(_pending_ids)
        _pending_ids.clear()
        return ids


def pending_event_ids() -> list[str]:
    with _lock:
        return list(_pending_ids)


def assign_staff_to_event(
    *,
    event: Event,
    staff: Staff,
    now: datetime,
) -> None:
    """Assign staff → compute real transit plan from their *true* current
    position (may be mid-transit from a prior return trip)."""
    now = _naive_utc(now)
    with _lock:
        origin_room = eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
        path, hop_times = eta.shortest_path(origin_room, event.room)

        event.status = "assigned"
        event.assigned_staff_id = staff.staff_id
        event.tending_until = None

        staff.status = "busy"
        staff.current_event_id = event.event_id
        staff.current_position = StaffPosition(room=event.room)
        staff.transit = TransitPlan(
            path=path,
            hop_times=hop_times,
            departure_time=now,
            mode="outbound",
        )
        staff.task_history = prune_task_history(staff.task_history, now)


def release_interrupted_event(staff: Staff, now: datetime) -> Optional[str]:
    """Tier 1 preempts staff busy on a lower-tier event. The interrupted
    event becomes pending again; the staff member snaps to their real
    current position (not the destination they never reached)."""
    now = _naive_utc(now)
    with _lock:
        if not staff.current_event_id:
            return None
        interrupted_id = staff.current_event_id
        interrupted = _events.get(interrupted_id)
        if interrupted and interrupted.status == "assigned":
            interrupted.status = "pending"
            interrupted.assigned_staff_id = None
            interrupted.tending_until = None  # tending was never completed
        staff.current_position = StaffPosition(
            room=eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
        )
        staff.transit = None
        staff.current_event_id = None
        staff.status = "available"
        return interrupted_id


def clear_assignment(event_id: str, now: datetime) -> Optional[Event]:
    """Manual override of the auto-lifecycle. Marks the event resolved
    immediately regardless of whether tending would have completed."""
    now = _naive_utc(now)
    with _lock:
        event = _events.get(event_id)
        if event is None:
            return None

        if event.assigned_staff_id:
            staff = _staff.get(event.assigned_staff_id)
            if staff:
                staff.task_history.append(
                    TaskHistoryEntry(event_id=event_id, completed_at=now)
                )
                staff.task_history = prune_task_history(staff.task_history, now)
                staff.status = "available"
                staff.current_event_id = None
                # Kick off return trip from wherever they really are now.
                actual_room = eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
                return_path, return_hops = eta.shortest_path(actual_room, staff.home_room)
                if len(return_path) > 1:
                    staff.transit = TransitPlan(
                        path=return_path,
                        hop_times=return_hops,
                        departure_time=now,
                        mode="return",
                    )
                    staff.current_position = StaffPosition(room=staff.home_room)
                else:
                    staff.transit = None
                    staff.current_position = StaffPosition(room=staff.home_room)

        event.status = "resolved"
        event.assigned_staff_id = None
        return event


def routing_lock() -> threading.RLock:
    return _lock
