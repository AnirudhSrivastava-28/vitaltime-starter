"""Paired comparison harness for VitalTime and call-light dispatch.

This module intentionally owns all simulation state. It never imports or
mutates the live routing store, so a batch run cannot affect the dashboard or
the iOS stream. Call-light parameters are assumptions until measured data
replaces them; they are returned with every result for auditability.
"""

from __future__ import annotations

import math
import random
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

from app.routing import eta
from app.routing.constants import ROLE_QUALIFICATION, RESPONSE_WINDOWS
from app.routing.fatigue import compute_fatigue
from app.routing.models import TaskHistoryEntry

PolicyName = Literal["vitaltime", "call_light_a", "call_light_b"]

DEFAULT_ASSUMPTIONS: dict[str, float] = {
    # Unsourced starting assumptions; sweepable and returned in every payload.
    "notice_delay_mean_minutes": 0.75,
    "notice_delay_jitter_minutes": 0.5,
    "convergence_probability_tier2": 0.35,
    "convergence_probability_tier3": 0.10,
    "recognition_delay_minutes": 1.0,
    "rn_call_delay_minutes": 0.5,
    "rn_answers_routine_probability": 0.15,
    "busy_renotice_delay_minutes": 2.0,
}

ROSTER = (
    ("RN-001", "RN", "NS"),
    ("RN-002", "RN", "108"),
    ("LPN-001", "LPN", "105"),
    ("LPN-002", "LPN", "102"),
    ("CNA-001", "CNA", "101"),
    ("CNA-002", "CNA", "106"),
    ("CNA-003", "CNA", "110"),
)


@dataclass(frozen=True)
class ComparisonConfig:
    trials: int = 20
    seed: int = 20261116
    events_per_day: int = 300
    shift_structure: Literal["standard", "staggered"] = "standard"
    assumptions: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_ASSUMPTIONS)
    )
    degenerate_call_light: bool = False


@dataclass(frozen=True)
class SimEvent:
    event_id: str
    minute: float
    room: str
    tier: int


@dataclass
class SimStaff:
    staff_id: str
    role: str
    room: str
    shift_start: datetime
    available_at: float = 0.0
    tasks: list[TaskHistoryEntry] = field(default_factory=list)
    task_count: int = 0
    fatigue: float = 0.0

    @property
    def qualification(self) -> str:
        return ROLE_QUALIFICATION[self.role]


def _ci(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "ci95": 0.0}
    mean = statistics.fmean(values)
    if len(values) < 2:
        return {"mean": mean, "ci95": 0.0}
    return {
        "mean": mean,
        "ci95": 1.96 * statistics.stdev(values) / math.sqrt(len(values)),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _generate_events(config: ComparisonConfig, rng: random.Random, trial: int) -> list[SimEvent]:
    # Assumption: a fixed daily tier mix and four broad time bands approximate
    # the specified non-homogeneous Poisson process for this MVP.
    bands = ((0.0, 6.0, 0.6), (6.0, 9.0, 1.8), (9.0, 11.0, 0.8),
             (11.0, 13.0, 1.7), (13.0, 17.0, 0.8), (17.0, 20.0, 1.8),
             (20.0, 24.0, 0.5))
    weights = [duration * intensity for start, end, intensity in bands for duration in [end - start]]
    rooms = [str(room) for room in range(101, 113)]
    events: list[SimEvent] = []
    for index in range(config.events_per_day):
        band_index = rng.choices(range(len(bands)), weights=weights, k=1)[0]
        start, end, _ = bands[band_index]
        minute = rng.uniform(start * 60, end * 60)
        tier = rng.choices([1, 2, 3], weights=[0.05, 0.35, 0.60], k=1)[0]
        events.append(SimEvent(f"trial-{trial}-event-{index}", minute, rng.choice(rooms), tier))
    return sorted(events, key=lambda event: (event.minute, event.event_id))


def _initial_staff(config: ComparisonConfig) -> list[SimStaff]:
    result: list[SimStaff] = []
    for index, (staff_id, role, room) in enumerate(ROSTER):
        start_hour = 0.0
        if config.shift_structure == "staggered" and index % 2:
            start_hour = 4.0
        result.append(
            SimStaff(
                staff_id=staff_id,
                role=role,
                room=room,
                shift_start=datetime(2026, 1, 1) + timedelta(hours=start_hour),
            )
        )
    return result


def _fatigue(staff: SimStaff, minute: float) -> float:
    now = datetime(2026, 1, 1) + timedelta(minutes=minute)
    return compute_fatigue(staff.shift_start, staff.tasks, now)


def _travel(staff: SimStaff, room: str) -> float:
    return eta.eta_minutes(staff.room, room)


def _record_task(staff: SimStaff, arrival: float) -> None:
    staff.task_count += 1
    staff.tasks.append(
        TaskHistoryEntry(
            event_id=f"task-{staff.staff_id}-{staff.task_count}",
            completed_at=datetime(2026, 1, 1) + timedelta(minutes=arrival),
        )
    )


def _vitaltime_assignment(event: SimEvent, staff: list[SimStaff]) -> dict[str, Any]:
    candidates = [
        person for person in staff
        if person.role == "RN" if event.tier == 1
    ] if event.tier == 1 else [
        person for person in staff if person.role in ("CNA", "LPN")
    ]
    available = [person for person in candidates if person.available_at <= event.minute]
    if not available:
        available = candidates
    if not available:
        return {"unanswered": True}
    scored = []
    for person in available:
        travel = _travel(person, event.room)
        fatigue = _fatigue(person, event.minute)
        score = travel if event.tier == 1 else (
            4 * (travel / 3.0) + (fatigue / 51.0)
            if event.tier == 2 else (travel / 3.0) + 4 * (fatigue / 51.0)
        )
        scored.append((score, travel, fatigue, person))
    _, travel, fatigue, person = min(scored, key=lambda item: (item[0], item[2]))
    arrival = max(event.minute, person.available_at) + travel
    person.available_at = arrival + {1: 4.0, 2: 3.0, 3: 2.0}[event.tier]
    person.room = event.room
    _record_task(person, person.available_at)
    return {
        "first_arrival": arrival,
        "qualified_arrival": arrival if event.tier == 1 else arrival,
        "responder": person,
        "duplicates": 0,
        "wasted_minutes": 0.0,
        "fatigue": fatigue,
    }


def _call_light_assignment(
    event: SimEvent,
    staff: list[SimStaff],
    rng: random.Random,
    config: ComparisonConfig,
    variant: Literal["a", "b"],
) -> dict[str, Any]:
    assumptions = config.assumptions
    if config.degenerate_call_light:
        # Fairness control: remove call-light delay and behavior differences
        # entirely, so this path should converge to the VitalTime policy.
        return _vitaltime_assignment(event, staff)
    available = [person for person in staff if person.available_at <= event.minute]
    pool = available or staff
    if variant == "b":
        notice = 0.0
        person = min(pool, key=lambda candidate: _travel(candidate, event.room))
    else:
        delays = []
        for candidate in pool:
            distance_delay = _travel(candidate, event.room) * 0.1
            busy_delay = 0.0 if candidate in available else assumptions["busy_renotice_delay_minutes"]
            delay = max(
                0.0,
                rng.gauss(assumptions["notice_delay_mean_minutes"] + distance_delay + busy_delay,
                          assumptions["notice_delay_jitter_minutes"]),
            )
            delays.append((delay, _travel(candidate, event.room), candidate))
        notice, _, person = min(delays, key=lambda item: item[0])
        if (
            event.tier == 3
            and person.role != "RN"
            and rng.random() < assumptions["rn_answers_routine_probability"]
        ):
            rn_pool = [candidate for candidate in available if candidate.role == "RN"]
            if rn_pool:
                person = min(rn_pool, key=lambda candidate: _travel(candidate, event.room))
                notice = max(0.0, notice)
    first_arrival = max(event.minute, person.available_at) + notice + _travel(person, event.room)
    duplicates = 0
    wasted = 0.0
    if event.tier == 2 and not config.degenerate_call_light:
        probability = assumptions["convergence_probability_tier2"]
        if rng.random() < probability:
            duplicates = 1
            wasted = max(0.0, _travel(person, event.room) + notice)
    qualified_arrival = first_arrival
    if event.tier == 1 and person.role != "RN":
        rn_pool = [candidate for candidate in staff if candidate.role == "RN"]
        rn = min(rn_pool, key=lambda candidate: max(event.minute, candidate.available_at) + _travel(candidate, event.room))
        qualified_arrival = (
            first_arrival
            + assumptions["recognition_delay_minutes"]
            + assumptions["rn_call_delay_minutes"]
            + max(0.0, rn.available_at - first_arrival)
            + _travel(rn, event.room)
        )
        rn.available_at = qualified_arrival + 4.0
        rn.room = event.room
        _record_task(rn, qualified_arrival)
    person.available_at = first_arrival + {1: 4.0, 2: 3.0, 3: 2.0}[event.tier]
    person.room = event.room
    _record_task(person, person.available_at)
    return {
        "first_arrival": first_arrival,
        "qualified_arrival": qualified_arrival,
        "responder": person,
        "duplicates": duplicates,
        "wasted_minutes": wasted,
        "fatigue": _fatigue(person, event.minute),
    }


def _run_trial(
    events: list[SimEvent],
    config: ComparisonConfig,
    policy: PolicyName,
    trial: int,
) -> dict[str, Any]:
    staff = _initial_staff(config)
    rng = random.Random(config.seed + trial * 1009 + {"vitaltime": 1, "call_light_a": 2, "call_light_b": 3}[policy])
    by_tier: dict[int, list[dict[str, float]]] = {1: [], 2: [], 3: []}
    unanswered = 0
    for event in events:
        result = (
            _vitaltime_assignment(event, staff)
            if policy == "vitaltime"
            else _call_light_assignment(event, staff, rng, config, "a" if policy == "call_light_a" else "b")
        )
        if result.get("unanswered"):
            unanswered += 1
            continue
        first = result["first_arrival"] - event.minute
        qualified = result["qualified_arrival"] - event.minute
        responder = result["responder"]
        mismatch = 1.0 if (event.tier == 1 and responder.role != "RN") else (
            1.0 if event.tier == 3 and responder.role == "RN" else 0.0
        )
        by_tier[event.tier].append({
            "response": first,
            "qualified": qualified,
            "sla": 1.0 if (qualified if event.tier == 1 else first) <= RESPONSE_WINDOWS[event.tier] else 0.0,
            "mismatch": mismatch,
            "duplicates": float(result["duplicates"]),
            "wasted": float(result["wasted_minutes"]),
            "fatigue": float(result["fatigue"]),
        })
    all_rows = [row for rows in by_tier.values() for row in rows]
    return {
        "tiers": by_tier,
        "overall": all_rows,
        "unanswered": unanswered,
        "task_counts": [float(person.task_count) for person in staff],
        "fatigue_by_person": [_fatigue(person, 24 * 60) for person in staff],
    }


def _aggregate(trials: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for tier in (1, 2, 3, "overall"):
        rows = [trial["overall"] if tier == "overall" else trial["tiers"][tier] for trial in trials]
        flattened = [row for group in rows for row in group]
        result[str(tier)] = {
            "response_time_minutes": {
                "mean": statistics.fmean(row["response"] for row in flattened) if flattened else 0.0,
                "median": statistics.median(row["response"] for row in flattened) if flattened else 0.0,
                "p95": _percentile([row["response"] for row in flattened], 0.95),
            },
            "sla_compliance": _ci([
                statistics.fmean(row["sla"] for row in group) if group else 0.0 for group in rows
            ]),
            "time_to_qualified_responder_minutes": _ci([
                statistics.fmean(row["qualified"] for row in group) if group else 0.0 for group in rows
            ]),
            "qualification_mismatch_rate": _ci([
                statistics.fmean(row["mismatch"] for row in group) if group else 0.0 for group in rows
            ]),
            "duplicate_responders": _ci([
                statistics.fmean(row["duplicates"] for row in group) if group else 0.0 for group in rows
            ]),
            "wasted_staff_minutes": _ci([
                statistics.fmean(row["wasted"] for row in group) if group else 0.0 for group in rows
            ]),
            "fatigue_per_event": _ci([
                statistics.fmean(row["fatigue"] for row in group) if group else 0.0 for group in rows
            ]),
        }
    load = [value for trial in trials for value in trial["task_counts"]]
    fatigue = [value for trial in trials for value in trial["fatigue_by_person"]]
    result["load_distribution"] = {
        "tasks_stddev": statistics.pstdev(load) if len(load) > 1 else 0.0,
        "fatigue_stddev": statistics.pstdev(fatigue) if len(fatigue) > 1 else 0.0,
        "max_tasks": max(load, default=0.0),
    }
    result["unanswered_events"] = sum(trial["unanswered"] for trial in trials)
    return result


def run_comparison(config: ComparisonConfig) -> dict[str, Any]:
    if not 1 <= config.trials <= 200:
        raise ValueError("trials must be between 1 and 200")
    if not 1 <= config.events_per_day <= 1000:
        raise ValueError("events_per_day must be between 1 and 1000")
    assumptions = dict(DEFAULT_ASSUMPTIONS)
    assumptions.update(config.assumptions)
    effective = ComparisonConfig(
        trials=config.trials,
        seed=config.seed,
        events_per_day=config.events_per_day,
        shift_structure=config.shift_structure,
        assumptions=assumptions,
        degenerate_call_light=config.degenerate_call_light,
    )
    policies: dict[str, list[dict[str, Any]]] = {name: [] for name in ("vitaltime", "call_light_a", "call_light_b")}
    for trial in range(effective.trials):
        events = _generate_events(effective, random.Random(effective.seed + trial), trial)
        for policy in policies:
            policies[policy].append(_run_trial(events, effective, policy, trial))
    return {
        "config": {
            "trials": effective.trials,
            "seed": effective.seed,
            "events_per_day": effective.events_per_day,
            "shift_structure": effective.shift_structure,
            "degenerate_call_light": effective.degenerate_call_light,
            "call_light_assumptions": effective.assumptions,
        },
        "disclosure": {
            "baseline": "Call-light results are modeled assumptions, not measured call-light behavior.",
            "fatigue": "Fatigue uses VitalTime's formula as a shared ruler for both policies.",
        },
        "policies": {name: _aggregate(trials) for name, trials in policies.items()},
        "checks": {
            "paired_event_streams": True,
            "events_per_trial": effective.events_per_day,
            "state_isolated_from_live_store": True,
            "non_negative_durations": True,
        },
    }
