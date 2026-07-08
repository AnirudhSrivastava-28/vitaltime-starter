"""Routing orchestration — sequential processing, interrupt logic (spec §6, §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.routing.filters import build_candidates
from app.routing.models import AssignmentResult, Event, Staff
from app.routing.scoring import select_candidate
from app.routing import store
from app.routing.tier import classify_tier


def _assign_event(event: Event, now: datetime) -> AssignmentResult:
    staff_pool = store.all_staff()
    events_by_id = {e.event_id: e for e in store.all_events()}

    candidates = build_candidates(staff_pool, event, events_by_id, now)
    chosen = select_candidate(candidates, event.tier)

    if chosen is None:
        event.status = "pending"
        event.assigned_staff_id = None
        store.enqueue_pending(event.event_id)
        return AssignmentResult(
            event_id=event.event_id,
            tier=event.tier,
            assigned_staff_id=None,
            eta=None,
            fatigue_score_at_assignment=None,
            status="pending",
        )

    if chosen.interruptible:
        interrupted_id = store.release_interrupted_event(chosen.staff, now)
        if interrupted_id:
            store.enqueue_pending(interrupted_id)

    store.assign_staff_to_event(event=event, staff=chosen.staff, now=now)

    return AssignmentResult(
        event_id=event.event_id,
        tier=event.tier,
        assigned_staff_id=chosen.staff.staff_id,
        eta=chosen.eta,
        fatigue_score_at_assignment=chosen.fatigue,
        status="assigned",
    )


def route_event(
    *,
    room: str,
    symptom_tags: list[str],
    submitted_at: datetime,
) -> AssignmentResult:
    """Process a single incoming event (caller holds routing lock)."""
    tier = classify_tier(symptom_tags)
    event = store.create_event(
        room=room,
        tier=tier,
        symptom_tags=symptom_tags,
        submitted_at=submitted_at,
    )
    return _assign_event(event, submitted_at)


def process_pending_events(now: Optional[datetime] = None) -> list[AssignmentResult]:
    """Rescore queued events in arrival order after staff availability changes."""
    now = now or datetime.utcnow()
    results: list[AssignmentResult] = []

    pending_ids = store.pop_pending_queue()
    pending_ids.sort(
        key=lambda eid: store.get_event(eid).submitted_at  # type: ignore[union-attr]
        if store.get_event(eid)
        else now
    )

    for event_id in pending_ids:
        event = store.get_event(event_id)
        if event is None or event.status != "pending":
            continue
        result = _assign_event(event, now)
        results.append(result)

    return results


def route_event_sequential(
    *,
    room: str,
    symptom_tags: list[str],
    submitted_at: datetime,
) -> AssignmentResult:
    """Public entry point — one event at a time (spec §6)."""
    with store.routing_lock():
        result = route_event(room=room, symptom_tags=symptom_tags, submitted_at=submitted_at)
        process_pending_events(submitted_at)
        return result


def clear_assignment(event_id: str, now: Optional[datetime] = None) -> Optional[AssignmentResult]:
    now = now or datetime.utcnow()
    with store.routing_lock():
        event = store.clear_assignment(event_id, now)
        if event is None:
            return None
        pending_results = process_pending_events(now)
        return pending_results[-1] if pending_results else None
