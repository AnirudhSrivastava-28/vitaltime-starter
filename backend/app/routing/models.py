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
    tending_until: Optional[datetime] = None
    eta: Optional[float] = None
    fatigue_score_at_assignment: Optional[float] = None
    # Best-effort prediction of which busy staff member will free up
    # soonest, set only while status == "pending" and every qualified
    # candidate is currently uninterruptible-busy (e.g. both RNs already
    # on other Tier 1s). This is NOT a reservation — the actual retry
    # when someone frees up still goes through normal scoring/priority
    # and may pick someone else if circumstances changed. Purely so the
    # UI can show "next up: RN-001, ~90s" instead of a black hole.
    # Cleared back to None the moment the event is actually assigned.
    predicted_next_staff_id: Optional[str] = None


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
    predicted_next_staff_id: Optional[str] = None
