"""Hard filters applied before scoring (spec §3)."""

from __future__ import annotations

from datetime import datetime

from app.routing import eta
from app.routing.constants import QUAL_RANK, RESPONSE_WINDOWS, TIER_REQUIRED_QUAL
from app.routing.fatigue import compute_fatigue
from app.routing.models import Candidate, Event, Staff
from app.routing.store import DEMO_TIME_SCALE


def meets_qualification(staff: Staff, tier: int) -> bool:
    required = TIER_REQUIRED_QUAL[tier]
    return QUAL_RANK[staff.qualification_level] >= QUAL_RANK[required]


def is_time_feasible(staff: Staff, event: Event, now: datetime) -> tuple[bool, float]:
    """Feasibility and ETA computed from the staff member's *true* current
    position — resolve_room accounts for in-progress transit rather than
    trusting a destination they haven't physically reached yet."""
    origin = eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
    travel_minutes = eta.eta_minutes(origin, event.room)
    window = RESPONSE_WINDOWS[event.tier]
    return travel_minutes <= window, travel_minutes


def is_available_for_tier(staff: Staff, tier: int) -> bool:
    if tier == 1:
        return True  # Tier 1 may interrupt (checked separately)
    return staff.status == "available"


def can_interrupt(staff: Staff, events_by_id: dict[str, Event]) -> bool:
    """Tier 1 may interrupt staff busy on a lower-tier (2/3) event."""
    if staff.status != "busy" or not staff.current_event_id:
        return False
    current = events_by_id.get(staff.current_event_id)
    if current is None or current.status != "assigned":
        return False
    return current.tier in (2, 3)


def build_candidates(
    staff_pool: list[Staff],
    event: Event,
    events_by_id: dict[str, Event],
    now: datetime,
    *,
    enforce_window: bool = True,
) -> list[Candidate]:
    """Apply hard filters and produce surviving candidates with ETA/fatigue."""
    candidates: list[Candidate] = []

    for staff in staff_pool:
        if not meets_qualification(staff, event.tier):
            continue

        feasible, eta_val = is_time_feasible(staff, event, now)
        if enforce_window and not feasible:
            continue

        interruptible = False
        if staff.status == "busy":
            if event.tier != 1 or not can_interrupt(staff, events_by_id):
                continue
            interruptible = True
        elif not is_available_for_tier(staff, event.tier):
            continue

        fatigue = compute_fatigue(staff.shift_start, staff.task_history, now)
        candidates.append(
            Candidate(staff=staff, eta=eta_val, fatigue=fatigue, interruptible=interruptible)
        )

    return candidates
