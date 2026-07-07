"""Fatigue scoring (shared formula for Tier 2/3, tiebreak for Tier 1)."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Iterable

from app.routing.constants import DECAY_RATE, TASK_HISTORY_MAX_AGE_HOURS, TASK_JUMP
from app.routing.models import TaskHistoryEntry


def hours_in_shift(shift_start: datetime, now: datetime) -> float:
    if shift_start.tzinfo is not None:
        shift_start = shift_start.replace(tzinfo=None)
    if now.tzinfo is not None:
        now = now.replace(tzinfo=None)
    delta = now - shift_start
    return max(0.0, delta.total_seconds() / 3600.0)


def elapsed_fatigue(hours: float) -> float:
    return hours * (hours + 1) / 2


def workload_fatigue(task_history: Iterable[TaskHistoryEntry], now: datetime) -> float:
    total = 0.0
    for entry in task_history:
        age_hours = (now - entry.completed_at).total_seconds() / 3600.0
        if age_hours < 0:
            continue
        total += TASK_JUMP * math.exp(-DECAY_RATE * age_hours)
    return total


def prune_task_history(
    task_history: list[TaskHistoryEntry], now: datetime
) -> list[TaskHistoryEntry]:
    cutoff = now - timedelta(hours=TASK_HISTORY_MAX_AGE_HOURS)
    return [entry for entry in task_history if entry.completed_at >= cutoff]


def compute_fatigue(
    shift_start: datetime,
    task_history: Iterable[TaskHistoryEntry],
    now: datetime,
) -> float:
    hours = hours_in_shift(shift_start, now)
    return elapsed_fatigue(hours) + workload_fatigue(task_history, now)
