"""Sequential dispatch orchestration (spec §6, §7).

REDESIGN CHANGES:

1. Server-clock routing. `route_event_sequential` treats `submitted_at`
   as metadata stored on the event record; every routing decision
   (feasibility, ETA, transit departure_time, fatigue-at-assignment)
   uses `datetime.utcnow()`. Client clock drift no longer causes the
   chip animation to sit parked at origin until wall-clock catches up
   to a client-supplied "future" departure.

2. Tier 1 best-effort. If the strict 2-min feasibility pass returns no
   candidates, run a second pass with the window relaxed. An emergency
   should never sit pending when a treatment-qualified staff exists
   somewhere in the facility. The 2-min window is an SLA target, not a
   gate that can drop emergencies.

3. Pending retry is now reactive and severity-ordered. `_process_pending`
   is registered as a store-side hook and fires whenever
   advance_simulation observes a staff transitioning busy→available
   with pending work in the queue. On retry, higher-tier events are
   tried first (so a pending Tier 1 beats a pending Tier 3 for the
   next freed staff member).

4. `tick(now)` exposed for the SSE loop and any other periodic caller:
   advance simulation + retry pending in one call. Handles the case
   where a returning staff passes through feasibility for a pending
   event without a discrete busy→available transition to trigger the
   hook.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from app.routing import filters, store, tier
from app.routing.models import AssignmentResult, Event
from app.routing.scoring import select_candidate


def _assign_event(event: Event, now: datetime) -> AssignmentResult:
    events_by_id = {e.event_id: e for e in store.all_events_raw()}
    staff_pool = store.all_staff_raw()

    # Strict pass first — respects response-window SLA.
    candidates = filters.build_candidates(
        staff_pool=staff_pool,
        event=event,
        events_by_id=events_by_id,
        now=now,
        enforce_window=True,
    )

    # Tier 1 best-effort fallback: if strict pass finds no one, retry
    # without the response-window filter. An emergency dispatches the
    # closest qualification-passing person even if they're beyond the
    # 2-min target, because "no one" is worse than "3 min away."
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


def _process_pending(now: datetime) -> Optional[AssignmentResult]:
    """Retry every pending event, higher-severity first.

    Order matters: if RN-001 just freed up and there are two pending
    events — a Tier 1 in room 108 and a Tier 3 in room 105 — the
    Tier 1 must be tried first, or the RN gets consumed by the routine
    task and the emergency sits pending again.
    """
    reassigned: Optional[AssignmentResult] = None
    pending_ids = store.pop_pending_queue()
    if not pending_ids:
        return None

    # Re-fetch and filter to actually-pending; sort by (tier, submitted_at).
    pending_events = []
    for eid in pending_ids:
        e = store.get_event(eid)
        if e is not None and e.status == "pending":
            pending_events.append(e)
    pending_events.sort(key=lambda e: (e.tier, e.submitted_at))

    for pending_event in pending_events:
        result = _assign_event(pending_event, now=now)
        if result.status == "assigned":
            reassigned = result
    return reassigned


def tick(now: Optional[datetime] = None) -> None:
    """Advance simulation and retry pending events.

    Called by the SSE loop every iteration, and internally after
    route_event / clear_assignment. Covers three cases:
      - Staff transitioned busy→available (advance_simulation fires the
        registered hook, which routes back through _process_pending).
      - A returning staff passes through feasibility for a pending
        event without a discrete transition (this call catches it on
        the next SSE tick).
      - Nothing changed (both calls are cheap no-ops).
    """
    now = now or datetime.utcnow()
    store.advance_simulation(now)
    if store.pending_event_ids():
        _process_pending(now)


def route_event_sequential(
    *,
    room: str,
    symptom_tags: list[str],
    submitted_at: datetime,
) -> AssignmentResult:
    """Classify → attempt assign → if unassigned, park pending.

    `submitted_at` is metadata on the event record only. All routing
    time reasoning uses server clock via `datetime.utcnow()` — this is
    what stops chip animation from stalling when the iOS clock drifts
    ahead of the server.
    """
    now = datetime.utcnow()
    store.advance_simulation(now)

    tier_value = tier.classify_tier(symptom_tags)
    event = store.create_event(
        room=room,
        tier=tier_value,
        symptom_tags=symptom_tags,
        submitted_at=submitted_at,
    )
    result = _assign_event(event, now=now)
    # If this assignment interrupted a lower-tier task, that task is
    # now pending — try to re-assign it before returning.
    _process_pending(now)
    return result


def clear_assignment(event_id: str, now: Optional[datetime] = None) -> Optional[AssignmentResult]:
    """Manual override — mark the event resolved and re-attempt any
    pending events."""
    now = now or datetime.utcnow()
    store.advance_simulation(now)
    event = store.clear_assignment(event_id, now)
    if event is None:
        return None
    return _process_pending(now)


# ---- register the pending-retry hook with the store -----------------------
# This is what makes the retry reactive to natural staff-freeing
# transitions (tending completes → advance_simulation sees the freed
# staff → fires this hook, which routes any queued pending event to the
# newly available person). Without this, pending events sit until the
# next external HTTP action triggers _process_pending — which is the
# root cause of your "event doesn't pop up until much later" observation.
store.register_pending_retry_hook(_process_pending)
