"""In-memory staff/event state for v1 routing (spec §5, §9).

REDESIGN CHANGES:

1. `register_pending_retry_hook(fn)` + hook invocation. `advance_simulation`
   now tracks whether any staff transitioned busy→available in this
   tick; if so and the pending queue is non-empty, fires the registered
   hook after releasing the lock. Engine registers `_process_pending` at
   import. This is what turns the pending queue from "sits until an
   external HTTP action triggers retry" into "reactively retries the
   instant a staff frees up."

2. `all_staff_raw()` / `all_events_raw()` — pure reads without triggering
   advance_simulation. Used by engine internals that have already
   advanced state; avoids re-entering advance_simulation from inside a
   routing decision (which could re-fire the hook mid-decision).
   Public read paths (`all_staff` / `all_events`) still tick.

3. `assign_staff_to_event` unchanged in signature but the caller now
   passes server-clock `now` (see engine.py), so departure_time is
   grounded in server time rather than client-supplied submitted_at.
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
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---- demo timing ------------------------------------------------------------

DEMO_TIME_SCALE = float(os.environ.get("VITALTIME_TIME_SCALE", "1.0"))

TENDING_MINUTES_BY_TIER: dict[int, float] = {
    1: 4.0,
    2: 3.0,
    3: 2.0,
}


def sim_minutes_to_real_seconds(sim_minutes: float) -> float:
    return (sim_minutes * 60.0) / DEMO_TIME_SCALE


def real_seconds_to_sim_minutes(real_seconds: float) -> float:
    return (real_seconds / 60.0) * DEMO_TIME_SCALE


def sim_timedelta(sim_minutes: float) -> timedelta:
    return timedelta(seconds=sim_minutes_to_real_seconds(sim_minutes))


# ---- change notification (SSE) ---------------------------------------------

_change_event: Optional[asyncio.Event] = None


def _get_change_event() -> asyncio.Event:
    global _change_event
    if _change_event is None:
        _change_event = asyncio.Event()
    return _change_event


def _signal_change() -> None:
    _get_change_event().set()


def _notify_sync_waiters() -> None:
    with _state_change_condition:
        _state_change_condition.notify_all()


def notify_state_change() -> None:
    _signal_change()
    _notify_sync_waiters()


async def wait_for_change(timeout: float) -> bool:
    ev = _get_change_event()
    try:
        await asyncio.wait_for(ev.wait(), timeout=timeout)
        ev.clear()
        return True
    except asyncio.TimeoutError:
        return False


# ---- pending-retry hook -----------------------------------------------------

_pending_retry_hook: Optional[Callable[[datetime], None]] = None


def register_pending_retry_hook(fn: Callable[[datetime], None]) -> None:
    """Engine registers `_process_pending` here at import time. Fired by
    advance_simulation whenever a staff transitions busy→available with
    a non-empty pending queue. Kept as a hook (rather than a direct
    import) to avoid a circular import: engine imports store, store
    can't also import engine.
    """
    global _pending_retry_hook
    _pending_retry_hook = fn


# ---- store ------------------------------------------------------------------

_lock = threading.RLock()
_state_change_condition = threading.Condition()
_staff: dict[str, Staff] = {}
_events: dict[str, Event] = {}
_pending_ids: list[str] = []

_SEED_SHIFT_HOURS_RANGE = (0.25, 7.5)
_SEED_TASK_COUNT_CHOICES = [0, 1, 2, 3]
_SEED_TASK_COUNT_WEIGHTS = [40, 30, 20, 10]
_SEED_TASK_AGE_MINUTES_RANGE = (5.0, 200.0)


def _seed_staff(now: datetime) -> None:
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

    Returns True if this call transitioned the staff from busy→available
    (i.e. tending completed) — the caller uses this to decide whether
    to fire the pending-retry hook.
    """
    freed_this_tick = False

    # (1) Outbound arrival → tending begins
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

    # (2) Tending complete → resolve → return trip begins → staff FREE
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
            freed_this_tick = True

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
                staff.transit = None
                staff.current_position = StaffPosition(room=staff.home_room)
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

    return freed_this_tick


def advance_simulation(now: Optional[datetime] = None) -> None:
    """Advance every staff member's lifecycle up to `now`.

    If any staff transitioned busy→available and the pending queue is
    non-empty, invoke the registered retry hook AFTER releasing the
    lock (the hook goes through engine._assign_event → store writes,
    which re-acquire the lock; RLock makes this safe either way, but
    releasing first keeps the critical section short and avoids
    surprising re-entry).
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


# ---- read paths -------------------------------------------------------------

def all_staff() -> list[Staff]:
    """Ticks the sim before returning. Public callers use this."""
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
    """Pure read WITHOUT ticking. Used by engine internals that have
    already advanced state; avoids re-entering advance_simulation from
    inside a routing decision (which would recursively re-fire the
    retry hook mid-decision)."""
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
    """Assign staff → compute transit plan from their *true* current
    position. `now` is server clock (see engine.py); departure_time on
    the transit plan is grounded in it, so client clock drift can't
    push the chip's animation start into the future."""
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
    """Tier 1 preempts staff busy on a lower-tier event."""
    now = _naive_utc(now)
    with _lock:
        if not staff.current_event_id:
            return None
        interrupted_id = staff.current_event_id
        interrupted = _events.get(interrupted_id)
        if interrupted and interrupted.status == "assigned":
            interrupted.status = "pending"
            interrupted.assigned_staff_id = None
            interrupted.tending_until = None
            interrupted.eta = None
            interrupted.fatigue_score_at_assignment = None

        staff.current_position = StaffPosition(
            room=eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
        )
        staff.transit = None
        staff.current_event_id = None
        staff.status = "available"
    notify_state_change()
    return interrupted_id


def clear_assignment(event_id: str, now: datetime) -> Optional[Event]:
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
