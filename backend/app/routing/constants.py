"""Routing engine constants (tunable defaults per spec §4)."""

TASK_JUMP = 3.0
DECAY_RATE = 0.7  # per hour
EPSILON = 0.15  # minutes — ETA tie threshold for Tier 1
ETA_SCALE = 3.0
FATIGUE_SCALE = 51.0

# Tier response windows in minutes (hard filter, §3)
RESPONSE_WINDOWS = {
    1: 2.0,
    2: 5.0,
    3: 10.0,
}

# Qualification hierarchy — higher rank satisfies lower-tier requirements
QUAL_RANK = {
    "task": 1,
    "assessment": 2,
    "treatment": 3,
}

TIER_REQUIRED_QUAL = {
    1: "treatment",
    2: "assessment",
    # "task" is the conceptual minimum for routine work; CNA/LPN roster
    # entries are both represented as "assessment", so filtering uses rank.
    3: "task",
}

ROLE_QUALIFICATION = {
    "RN": "treatment",
    "LPN": "assessment",
    "CNA": "assessment",
}

TASK_HISTORY_MAX_AGE_HOURS = 4.0
