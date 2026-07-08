"""Sequential dispatch orchestration (spec §6, §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.routing import filters, store, tier
from app.routing.models import AssignmentResult, Event
from app.routing.scoring import select_candidate


def _assign_event(event: Event, now: datetime) -> AssignmentResult:
    events_by_id = {e.event_id: e for e in store.all_events()}
    candidates = filters.build_candidates(
        staff_pool=store.all_staff(),
        event=event,
        events_by_id=events_by_id,
        now=now,
    )

    chosen = select_candidate(candidates, tier=event.tier) if candidates else None
    if chosen is None:
        event.status = "pending"
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


def route_event_sequential(
    *,
    room: str,
    symptom_tags: list[str],
    submitted_at: datetime,
) -> AssignmentResult:
    """Classify → attempt assign → if unassigned, park pending."""
    # Bring state current before making a routing decision — a returning
    # nurse who has actually reached home in the last few seconds should
    # be a valid candidate for this new event.
    store.advance_simulation(submitted_at)

    tier_value = tier.classify_tier(symptom_tags)
    event = store.create_event(
        room=room,
        tier=tier_value,
        symptom_tags=symptom_tags,
        submitted_at=submitted_at,
    )
    result = _assign_event(event, now=submitted_at)
    # If this assignment interrupted a lower-tier task, that task is now
    # pending — try to re-assign it before returning.
    _process_pending(submitted_at)
    return result


def _process_pending(now: datetime) -> Optional[AssignmentResult]:
    """Re-attempt any queued pending events; return the last successful
    assignment (if any) so the caller can surface it to the client."""
    reassigned: Optional[AssignmentResult] = None
    for pending_id in store.pop_pending_queue():
        pending_event = store.get_event(pending_id)
        if pending_event is None or pending_event.status != "pending":
            continue
        result = _assign_event(pending_event, now=now)
        if result.status == "assigned":
            reassigned = result
    return reassigned


def clear_assignment(event_id: str) -> Optional[AssignmentResult]:
    """Manual override — mark the event resolved and re-attempt any
    pending events (which may now find a candidate)."""
    now = datetime.utcnow()
    store.advance_simulation(now)
    event = store.clear_assignment(event_id, now)
    if event is None:
        return None
    return _process_pending(now)
