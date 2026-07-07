"""Tier-specific scoring and assignment selection (spec §4)."""

from __future__ import annotations

from typing import Optional

from app.routing.constants import EPSILON, ETA_SCALE, FATIGUE_SCALE
from app.routing.models import Candidate


def score_tier2(candidate: Candidate) -> float:
    return 4 * (candidate.eta / ETA_SCALE) + 1 * (candidate.fatigue / FATIGUE_SCALE)


def score_tier3(candidate: Candidate) -> float:
    return 1 * (candidate.eta / ETA_SCALE) + 4 * (candidate.fatigue / FATIGUE_SCALE)


def select_candidate(candidates: list[Candidate], tier: int) -> Optional[Candidate]:
    if not candidates:
        return None

    if tier == 1:
        best_eta = min(c.eta for c in candidates)
        tied = [c for c in candidates if c.eta <= best_eta + EPSILON]
        if len(tied) == 1:
            return tied[0]
        return min(tied, key=lambda c: c.fatigue)

    score_fn = score_tier2 if tier == 2 else score_tier3
    return min(candidates, key=score_fn)
