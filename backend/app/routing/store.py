"""In-memory staff/event state for v1 routing (spec §5, §9)."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional

from app.routing.constants import ROLE_QUALIFICATION
from app.routing.fatigue import prune_task_history
from app.routing.models import Event, Staff, StaffPosition, TaskHistoryEntry

_lock = threading.RLock()
_staff: dict[str, Staff] = {}
_events: dict[str, Event] = {}
_pending_ids: list[str] = []


def _seed_staff(now: datetime) -> None:
    """Demo staff roster for simulation/testing."""
    shift_start = now - timedelta(hours=3)
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
        _staff[staff_id] = Staff(
            staff_id=staff_id,
            role=role,  # type: ignore[arg-type]
            qualification_level=ROLE_QUALIFICATION[role],  # type: ignore[index]
            shift_start=shift_start,
            current_position=StaffPosition(room=room),
        )


def _ensure_seeded() -> None:
    if not _staff:
        _seed_staff(datetime.utcnow())


def reset_simulation() -> None:
    with _lock:
        _staff.clear()
        _events.clear()
        _pending_ids.clear()
        _seed_staff(datetime.utcnow())


def all_staff() -> list[Staff]:
    _ensure_seeded()
    with _lock:
        return list(_staff.values())


def all_events() -> list[Event]:
    with _lock:
        return list(_events.values())


def get_event(event_id: str) -> Optional[Event]:
    with _lock:
        return _events.get(event_id)


def get_staff(staff_id: str) -> Optional[Staff]:
    _ensure_seeded()
    with _lock:
        return _staff.get(staff_id)


def create_event(
    *,
    room: str,
    tier: int,
    symptom_tags: list[str],
    submitted_at: datetime,
    escalated_from: Optional[str] = None,
) -> Event:
    _ensure_seeded()
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
    with _lock:
        event.status = "assigned"
        event.assigned_staff_id = staff.staff_id
        staff.status = "busy"
        staff.current_event_id = event.event_id
        staff.current_position = StaffPosition(room=event.room)
        staff.task_history = prune_task_history(staff.task_history, now)


def release_interrupted_event(staff: Staff) -> Optional[str]:
    """Return interrupted event_id and mark it pending again."""
    with _lock:
        if not staff.current_event_id:
            return None
        interrupted_id = staff.current_event_id
        interrupted = _events.get(interrupted_id)
        if interrupted and interrupted.status == "assigned":
            interrupted.status = "pending"
            interrupted.assigned_staff_id = None
        staff.current_event_id = None
        staff.status = "available"
        return interrupted_id


def clear_assignment(event_id: str, now: datetime) -> Optional[Event]:
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

        event.status = "resolved"
        event.assigned_staff_id = None
        return event


def routing_lock() -> threading.RLock:
    return _lock
