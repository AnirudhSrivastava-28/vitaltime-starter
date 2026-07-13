"""Sequential dispatch orchestration (spec §6, §7)."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.routing import filters, store, tier
from app.routing.models import AssignmentResult, Event
from app.routing.scoring import select_candidate


def _predict_next_available_staff(event: Event, now: datetime) -> Optional[str]:
    """Best-effort prediction of which qualified-but-busy staff member
    will free up soonest, for pending events where every candidate is
    currently uninterruptible-busy (the only way this happens today:
    a Tier 1 arriving while every RN is already on another Tier 1).

    NOT a reservation — when someone actually frees up, the normal
    scoring/priority path in _process_pending runs as usual and may pick
    a different (or no) candidate if circumstances changed in the
    meantime (e.g. the predicted staff member gets interrupted by a
    different emergency first). This exists purely so pending events
    aren't a black hole to the UI — "next up: RN-001, ~90s" instead of
    just "pending" with no indication of what happens next. Recomputed
    on every retry, so it stays live rather than going stale.
    """
    events_by_id = {e.event_id: e for e in store.all_events_raw()}
    best_staff_id: Optional[str] = None
    best_free_time: Optional[datetime] = None

    for staff in store.all_staff_raw():
        if not filters.meets_qualification(staff, event.tier):
            continue
        if staff.status != "busy" or not staff.current_event_id or staff.transit is None:
            continue

        current_event = events_by_id.get(staff.current_event_id)
        if current_event is None:
            continue

        if current_event.tending_until is not None:
            # Already arrived and tending — this is exactly when they'll
            # become available again.
            free_time = current_event.tending_until
        else:
            # Still en route — estimate arrival + this event's tending
            # duration, same formula store.py's own lifecycle sim uses.
            arrival = staff.transit.departure_time + store.sim_timedelta(
                staff.transit.hop_times[-1]
            )
            tending_minutes = store.TENDING_MINUTES_BY_TIER.get(current_event.tier, 3.0)
            free_time = arrival + store.sim_timedelta(tending_minutes)

        if best_free_time is None or free_time < best_free_time:
            best_free_time = free_time
            best_staff_id = staff.staff_id

    return best_staff_id


def _assign_event(event: Event, now: datetime) -> AssignmentResult:
    events_by_id = {e.event_id: e for e in store.all_events_raw()}
    staff_pool = store.all_staff_raw()

    candidates = filters.build_candidates(
        staff_pool=staff_pool,
        event=event,
        events_by_id=events_by_id,
        now=now,
        # Tier 2/3: try the exact-qualification pool first (CNA/LPN) so a
        # fresh, nearby RN doesn't out-score them on ETA/fatigue alone —
        # RN capacity should default to reserved for Tier 1. Tier 1 is
        # unaffected: "treatment" already only exactly matches RN, so
        # this flag is a no-op there.
        prefer_exact_qualification=(event.tier != 1),
    )

    if not candidates and event.tier != 1:
        # No CNA/LPN available at all — better to send the idle RN than
        # leave a Validation/Normal event unassigned. Last resort, not
        # the default.
        candidates = filters.build_candidates(
            staff_pool=staff_pool,
            event=event,
            events_by_id=events_by_id,
            now=now,
            prefer_exact_qualification=False,
        )

    if not candidates and event.tier == 1:
        candidates = filters.build_candidates(
            staff_pool=staff_pool,
            event=event,
            events_by_id=events_by_id,
            now=now,
            enforce_window=False,
        )

    chosen = select_candidate(candidates, tier=event.tier) if candidates else None
    if chosen is None:
        event.status = "pending"
        event.predicted_next_staff_id = _predict_next_available_staff(event, now)
        store.enqueue_pending(event.event_id)
        return AssignmentResult(
            event_id=event.event_id,
            tier=event.tier,
            assigned_staff_id=None,
            eta=None,
            fatigue_score_at_assignment=None,
            status="pending",
            predicted_next_staff_id=event.predicted_next_staff_id,
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
    # No longer relevant once actually assigned — clear the prediction
    # rather than leaving a stale value from a previous pending stretch.
    event.predicted_next_staff_id = None

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
