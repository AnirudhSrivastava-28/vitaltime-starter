"""In-memory staff/event state for v1 routing (spec §5, §9).

Adds an authoritative simulation lifecycle: assigned events auto-arrive,
auto-tend, auto-resolve, and staff auto-return home. All state transitions
are driven by wall-clock elapsed time against DEMO_TIME_SCALE, so the
same clock the client uses for animation is the same clock the backend
uses for state changes — client and backend can never disagree about
which phase of the lifecycle a staff member is in.

Also exposes a change-notification primitive (_signal_change /
wait_for_change) used by the SSE endpoint in routers/routing.py to push
state to clients the instant something changes, instead of clients
polling. Two kinds of changes exist here, and both need to wake
subscribers: explicit writes (a new event submitted, an assignment
cleared) AND lazy time-based transitions that only get discovered when
advance_simulation() runs (a staff member arriving, tending completing,
etc.) — the latter is why the SSE loop still ticks on a short timeout
even though it's primarily signal-driven, rather than only ever waiting
on explicit writes.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

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
# minutes, a 4-minute tending window takes 4 real minutes. Override via
# env var if a sped-up demo reel is ever wanted again.
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


# ---- change notification (for SSE) ------------------------------------------

# Created lazily rather than at import time: asyncio.Event() is fine to
# construct before a loop exists (it no longer binds to a loop at
# construction as of Python 3.10), but lazy creation keeps this module
# importable in any context (e.g. plain sync test code) without assuming
# an event loop is already running.
_change_event: Optional[asyncio.Event] = None


def _get_change_event() -> asyncio.Event:
    global _change_event
    if _change_event is None:
        _change_event = asyncio.Event()
    return _change_event


def _signal_change() -> None:
    """Wake any SSE subscribers waiting in wait_for_change(). Called from
    every write path below, and from _advance_single_staff whenever a
    lazy time-based transition actually fires. Cheap and safe to call
    even with zero subscribers."""
    _get_change_event().set()


def _notify_sync_waiters() -> None:
    with _state_change_condition:
        _state_change_condition.notify_all()


def notify_state_change() -> None:
    _signal_change()
    _notify_sync_waiters()


async def wait_for_change(timeout: float) -> bool:
    """Block until _signal_change() fires or `timeout` seconds elapse.

    Returns True if woken by an explicit signal, False on timeout. The
    caller (the SSE stream generator) re-checks state either way — the
    timeout exists specifically to catch time-based transitions that
    happen with nobody having explicitly called _signal_change() for them
    yet, by forcing a periodic advance_simulation() regardless."""
    ev = _get_change_event()
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        ev.clear()
        return True
    except asyncio.TimeoutError:
        return False


# ---- store ------------------------------------------------------------------

_lock = threading.RLock()
_state_change_condition = threading.Condition()
_staff: dict[str, Staff] = {}
_events: dict[str, Event] = {}
_pending_ids: list[str] = []
_pending_retry_hook: Optional[Callable[[datetime], None]] = None

# Bounds for the randomized seed scenario — see _seed_staff.
_SEED_SHIFT_HOURS_RANGE = (0.25, 7.5)          # within a plausible 8h shift
_SEED_TASK_COUNT_CHOICES = [0, 1, 2, 3]
_SEED_TASK_COUNT_WEIGHTS = [40, 30, 20, 10]     # most staff start light
_SEED_TASK_AGE_MINUTES_RANGE = (5.0, 200.0)     # stays under the 4h prune window


def _seed_staff(now: datetime) -> None:
    """Demo staff roster for simulation/testing. Each staff member's initial
    room is also their home_room — where they return to after events resolve.

    Both fatigue inputs are randomized on every call (i.e. every cold start
    and every /reset-simulation), rather than fixed, so every reset gives a
    genuinely different staffing-fatigue scenario to route against instead
    of the same "who's freshest" call every time.
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
    notify_state_change()


# ---- lifecycle simulation ---------------------------------------------------

def _advance_single_staff(staff: Staff, now: datetime) -> bool:
    """Fire any state transitions that should have happened by `now`.

    Three transitions per staff member per tick:
      (1) Outbound arrival → tending starts (tending_until is set)
      (2) Tending complete → event resolves; return transit begins
      (3) Return arrival   → transit cleared; staff idle at home

    Returns True if the staff member transitioned from busy to available.
    Each transition is idempotent — calling with the same `now` twice is
    safe. A manual /clear-assignment before (2) short-circuits (2)+(3).
    Each branch calls _signal_change() only when it actually fires, so
    the SSE stream wakes immediately on a real transition rather than
    waiting out its periodic safety-net timeout.
    """
    freed = False
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
                _signal_change()

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
            freed = True
            _signal_change()

    # (3) Return arrival → idle
    if (
        staff.status == "available"
        and staff.transit is not None
        and staff.transit.mode == "return"
    ):
        return_arrival = staff.transit.departure_time + sim_timedelta(staff.transit.hop_times[-1])
        if now >= return_arrival:
            staff.transit = None
            _signal_change()

    return freed


def register_pending_retry_hook(fn: callable[[datetime], None]) -> None:
    global _pending_retry_hook
    _pending_retry_hook = fn


def advance_simulation(now: Optional[datetime] = None) -> None:
    """Advance every staff member's lifecycle up to `now`.

    Called at the top of every read path (all_staff, all_events) so the
    state a client sees is always current — no background threads needed,
    no race between reader and writer, no dependency on how often clients
    poll. The SSE stream (routers/routing.py) also calls this directly on
    every tick of its own loop, since it's the thing now responsible for
    discovering lazy time-based transitions in the first place — nothing
    else reads state on a timer anymore once clients stop polling.
    """
    now = _naive_utc(now or datetime.utcnow())
    freed_any = False
    with _lock:
        for staff in _staff.values():
            if _advance_single_staff(staff, now):
                freed_any = True
        pending_exists = bool(_pending_ids)

    if freed_any and pending_exists and _pending_retry_hook is not None:
        _pending_retry_hook(now)


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


def all_staff_raw() -> list[Staff]:
    _ensure_seeded()
    with _lock:
        return list(_staff.values())


def all_events_raw() -> list[Event]:
    _ensure_seeded()
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
    _signal_change()
    return event


def enqueue_pending(event_id: str) -> None:
    with _lock:
        if event_id not in _pending_ids:
            _pending_ids.append(event_id)
    notify_state_change()


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
    eta_minutes: Optional[float] = None,
    fatigue_score_at_assignment: Optional[float] = None,
) -> None:
    """Assign staff → compute real transit plan from their *true* current
    position (may be mid-transit from a prior return trip).

    eta_minutes/fatigue are the scoring inputs that justified this
    assignment (from the Candidate the engine selected) — stored on the
    Event itself so /events and /dashboard-state (and the SSE stream)
    always have them, not just the single RouteEventResponse returned at
    submission time."""
    now = _naive_utc(now)
    with _lock:
        origin_room = eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
        path, hop_times = eta.shortest_path(origin_room, event.room)

        event.status = "assigned"
        event.assigned_staff_id = staff.staff_id
        event.eta = eta_minutes
        event.fatigue_score_at_assignment = fatigue_score_at_assignment
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
    notify_state_change()


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
            # Stale numbers from the old assignment would misrepresent an
            # event that's no longer actually assigned to anyone.
            interrupted.eta = None
            interrupted.fatigue_score_at_assignment = None

        # Always snap the staff to their real current position and clear
        # any transit when they are interrupted, even if the interrupted
        # event record is missing or in an unexpected state. This
        # preserves the invariant that a preempted staff is available
        # and not mid-transit to an old destination.
        staff.current_position = StaffPosition(
            room=eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
        )
        staff.transit = None
        staff.current_event_id = None
        staff.status = "available"
    notify_state_change()
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
        event.eta = None
        event.fatigue_score_at_assignment = None
    notify_state_change()
    return event


def wait_for_state_change(timeout: float = 60.0) -> bool:
    with _state_change_condition:
        return _state_change_condition.wait(timeout=timeout)


def routing_lock() -> threading.RLock:
    return _lock
