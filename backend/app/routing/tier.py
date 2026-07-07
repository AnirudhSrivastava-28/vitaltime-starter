"""Tier classification from symptom tags (keyword lookup, spec §2)."""

from __future__ import annotations

from typing import Iterable

# Longer / more specific phrases should appear in the appropriate tier list.
# When multiple tags match, the highest-severity (lowest tier number) wins.
TIER_KEYWORDS: dict[int, tuple[str, ...]] = {
    1: (
        "not breathing",
        "unresponsive",
        "severe distress",
        "cardiac arrest",
        "choking",
        "no pulse",
        "seizure",
        "anaphylaxis",
    ),
    2: (
        "possible fall",
        "uncertain",
        "confused",
        "dizzy",
        "chest pain",
        "shortness of breath",
        "high fever",
        "altered mental status",
        "possible stroke",
        "unknown complaint",
    ),
    3: (
        "needs bandage",
        "minor bleeding",
        "escort",
        "routine",
        "medication refill",
        "bathroom assist",
        "comfort check",
        "linen change",
    ),
}


def classify_tier(symptom_tags: Iterable[str]) -> int:
    """Classify an event into exactly one tier before scoring."""
    normalized = [tag.strip().lower() for tag in symptom_tags if tag and tag.strip()]
    if not normalized:
        return 2  # ambiguous input → validation tier (safe default)

    best_tier = 3
    for tag in normalized:
        for tier in (1, 2, 3):
            if any(keyword in tag for keyword in TIER_KEYWORDS[tier]):
                best_tier = min(best_tier, tier)
                break

    return best_tier
