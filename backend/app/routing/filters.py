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


def meets_qualification_exact(staff: Staff, tier: int) -> bool:
    """True if staff isn't over-qualified for this tier's actual roster.

    NOTE: this can't be a literal string match against
    TIER_REQUIRED_QUAL — ROLE_QUALIFICATION maps both CNA and LPN to
    "assessment", and no role is ever assigned the literal "task" level.
    TIER_REQUIRED_QUAL[3] == "task" therefore matches zero real staff by
    string equality; a naive exact-match implementation silently falls
    through to the unrestricted pool every time for Tier 3, which is
    what happened on the first pass of this fix. The practically correct
    meaning of "exact" here is "not strictly over-qualified": exclude
    only staff whose rank exceeds the highest rank actually used by any
    non-RN role ("assessment"), so RN is reserved for Tier 1 while CNA
    and LPN both remain eligible for Tier 2 and Tier 3 as intended.
    """
    if tier == 1:
        return staff.qualification_level == TIER_REQUIRED_QUAL[1]
    return QUAL_RANK[staff.qualification_level] <= QUAL_RANK["assessment"]


def is_time_feasible(staff: Staff, event: Event, now: datetime) -> tuple[bool, float]:
    origin = eta.resolve_room(staff, now, time_scale=DEMO_TIME_SCALE)
    travel_minutes = eta.eta_minutes(origin, event.room)
    window = RESPONSE_WINDOWS[event.tier]
    return travel_minutes <= window, travel_minutes


def is_available_for_tier(staff: Staff, tier: int) -> bool:
    if tier == 1:
        return True
    return staff.status == "available"


def can_interrupt(staff: Staff, events_by_id: dict[str, Event]) -> bool:
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
    prefer_exact_qualification: bool = False,
) -> list[Candidate]:
    candidates: list[Candidate] = []

    for staff in staff_pool:
        if not meets_qualification(staff, event.tier):
            continue

        if prefer_exact_qualification and not meets_qualification_exact(staff, event.tier):
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
