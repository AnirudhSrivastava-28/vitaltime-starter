"""Sequential dispatch orchestration (spec §6, §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.routing import filters, store, tier
from app.routing.models import AssignmentResult, Event
from app.routing.scoring import select_candidate


def _assign_event(event: Event, now: datetime) -> AssignmentResult:
    events_by_id = {e.event_id: e for e in store.all_events_raw()}
    candidates = filters.build_candidates(
        staff_pool=store.all_staff_raw(),
        event=event,
        events_by_id=events_by_id,
        now=now,
    )

    if not candidates and event.tier == 1:
        candidates = filters.build_candidates(
            staff_pool=store.all_staff_raw(),
            event=event,
            events_by_id=events_by_id,
            now=now,
            enforce_window=False,
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

    store.assign_staff_to_event(
        event=event,
        staff=chosen.staff,
        now=now,
        eta_minutes=chosen.eta,
        fatigue_score_at_assignment=chosen.fatigue,
    )

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
    now = datetime.utcnow()
    tick(now)

    tier_value = tier.classify_tier(symptom_tags)
    event = store.create_event(
        room=room,
        tier=tier_value,
        symptom_tags=symptom_tags,
        submitted_at=submitted_at,
    )
    result = _assign_event(event, now=now)
    # If this assignment interrupted a lower-tier task, that task is now
    # pending — try to re-assign it before returning.
    _process_pending(now)
    return result


def _process_pending(now: datetime) -> Optional[AssignmentResult]:
    """Re-attempt any queued pending events; return the last successful
    assignment (if any) so the caller can surface it to the client."""
    reassigned: Optional[AssignmentResult] = None
    pending_ids = store.pop_pending_queue()
    pending_events = [store.get_event(eid) for eid in pending_ids]
    pending_events = [e for e in pending_events if e is not None and e.status == "pending"]
    pending_events.sort(key=lambda e: (e.tier, e.submitted_at))

    for pending_event in pending_events:
        result = _assign_event(pending_event, now=now)
        if result.status == "assigned":
            reassigned = result
    return reassigned


def tick(now: Optional[datetime] = None) -> None:
    now = now or datetime.utcnow()
    store.advance_simulation(now)
    if store.pending_event_ids():
        _process_pending(now)


def clear_assignment(event_id: str, now: Optional[datetime] = None) -> Optional[AssignmentResult]:
    """Manual override — mark the event resolved and re-attempt any
    pending events (which may now find a candidate).

    Accepts an optional `now` to allow tests and callers to advance the
    simulation to a specific timestamp before clearing an assignment.
    """
    now = now or datetime.utcnow()
    tick(now)
    event = store.clear_assignment(event_id, now)
    if event is None:
        return None
    return _process_pending(now)


def reset_simulation() -> None:
    """Reinitialize the backend simulation state and notify clients.

    The backend is the sole source of truth for scenario state. Any
    client-side views should refresh from the live stream rather than
    retaining stale local scenarios across resets.
    """
    store.reset_simulation()


store.register_pending_retry_hook(_process_pending)
