"""Data models for the routing engine (spec §5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional

StaffRole = Literal["RN", "LPN", "CNA"]
QualificationLevel = Literal["treatment", "assessment", "task"]
StaffStatus = Literal["available", "busy"]
EventStatus = Literal["pending", "assigned", "resolved", "escalated"]


@dataclass
class TaskHistoryEntry:
    event_id: str
    completed_at: datetime


@dataclass
class StaffPosition:
    room: str


@dataclass
class TransitPlan:
    """A currently-in-progress move across the facility graph.

    path/hop_times come from eta.shortest_path() — the same function used
    for feasibility filtering — so the animation this drives is never a
    client-side approximation; it's a rendering of the real routing
    decision.

    hop_times are in *sim minutes* (unscaled routing units, as used by the
    scoring/filtering code). The API response layer scales these to real
    wall-clock seconds via DEMO_TIME_SCALE for the client.
    """
    path: list[str]
    hop_times: list[float]
    departure_time: datetime
    mode: Literal["outbound", "return"] = "outbound"


@dataclass
class Staff:
    staff_id: str
    role: StaffRole
    qualification_level: QualificationLevel
    shift_start: datetime
    current_position: StaffPosition
    home_room: str = "NS"
    task_history: list[TaskHistoryEntry] = field(default_factory=list)
    status: StaffStatus = "available"
    current_event_id: Optional[str] = None
    transit: Optional[TransitPlan] = None


@dataclass
class Event:
    event_id: str
    room: str
    tier: int
    symptom_tags: list[str]
    submitted_at: datetime
    status: EventStatus = "pending"
    assigned_staff_id: Optional[str] = None
    escalated_from: Optional[str] = None
    # Set by the sim engine when the assigned staff arrives at the room;
    # the event auto-resolves once wall-clock time passes this timestamp.
    tending_until: Optional[datetime] = None
    # Snapshot of the routing decision at the moment of assignment. Stored
    # here (not just returned once in RouteEventResponse) so that any later
    # read of this event — /events, /dashboard-state, a fresh poll after
    # the client that submitted it is long gone — still has the numbers
    # that justified the decision, instead of them only existing in a
    # single HTTP response nobody may still be holding onto. Cleared back
    # to None if the event is interrupted and returned to pending, since a
    # stale eta/fatigue from the old assignment would be misleading once
    # it's no longer actually assigned to anyone.
    eta: Optional[float] = None
    fatigue_score_at_assignment: Optional[float] = None


@dataclass
class Candidate:
    staff: Staff
    eta: float
    fatigue: float
    interruptible: bool = False


@dataclass
class AssignmentResult:
    event_id: str
    tier: int
    assigned_staff_id: Optional[str]
    eta: Optional[float]
    fatigue_score_at_assignment: Optional[float]
    status: EventStatus
