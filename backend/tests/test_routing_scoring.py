"""Validate routing scoring logic with pandas DataFrames (spec §9)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from app.routing.constants import (
    DECAY_RATE,
    EPSILON,
    ETA_SCALE,
    FATIGUE_SCALE,
    TASK_JUMP,
)
from app.routing.fatigue import compute_fatigue, elapsed_fatigue, workload_fatigue
from app.routing.models import Candidate, Staff, StaffPosition, TaskHistoryEntry
from app.routing.scoring import score_tier2, score_tier3, select_candidate
from app.routing.tier import classify_tier
from app.routing import engine, store


NOW = datetime(2026, 7, 6, 14, 0, 0)


def _staff(staff_id: str, role: str = "RN", room: str = "NS", hours: float = 3.0) -> Staff:
    return Staff(
        staff_id=staff_id,
        role=role,  # type: ignore[arg-type]
        qualification_level={"RN": "treatment", "LPN": "assessment", "CNA": "assessment"}[role],  # type: ignore[arg-type]
        shift_start=NOW - timedelta(hours=hours),
        current_position=StaffPosition(room=room),
    )


class TestTierClassification:
    def test_tier1_emergency_keywords(self):
        assert classify_tier(["not breathing"]) == 1
        assert classify_tier(["patient unresponsive"]) == 1

    def test_tier2_validation_keywords(self):
        assert classify_tier(["possible fall"]) == 2

    def test_tier3_routine_keywords(self):
        assert classify_tier(["needs bandage"]) == 3

    def test_highest_severity_wins(self):
        assert classify_tier(["needs bandage", "not breathing"]) == 1

    def test_empty_defaults_to_tier2(self):
        assert classify_tier([]) == 2


class TestFatigueFormulas:
    def test_elapsed_fatigue_quadratic(self):
        df = pd.DataFrame({"hours": [0, 2, 4, 8]})
        df["elapsed"] = df["hours"].apply(elapsed_fatigue)
        df["expected"] = df["hours"] * (df["hours"] + 1) / 2
        pd.testing.assert_series_equal(df["elapsed"], df["expected"], check_names=False)

    def test_workload_fatigue_decay(self):
        history = [
            TaskHistoryEntry("e1", NOW - timedelta(hours=1)),
            TaskHistoryEntry("e2", NOW - timedelta(hours=3)),
        ]
        total = workload_fatigue(history, NOW)
        import math

        expected = TASK_JUMP * math.exp(-DECAY_RATE * 1) + TASK_JUMP * math.exp(-DECAY_RATE * 3)
        assert abs(total - expected) < 1e-9

    def test_fatigue_at_8h_shift(self):
        staff = _staff("RN-001", hours=8.0)
        fatigue = compute_fatigue(staff.shift_start, [], NOW)
        assert abs(fatigue - 36.0) < 1e-9  # 8 * 9 / 2


class TestScoringWithDataFrames:
    def test_tier1_picks_lowest_eta(self):
        candidates = [
            Candidate(_staff("A", room="101"), eta=1.0, fatigue=10.0),
            Candidate(_staff("B", room="108"), eta=2.5, fatigue=5.0),
        ]
        chosen = select_candidate(candidates, tier=1)
        assert chosen is not None
        assert chosen.staff.staff_id == "A"

    def test_tier1_fatigue_tiebreak_within_epsilon(self):
        candidates = [
            Candidate(_staff("A"), eta=1.0, fatigue=20.0),
            Candidate(_staff("B"), eta=1.0 + EPSILON, fatigue=5.0),
        ]
        chosen = select_candidate(candidates, tier=1)
        assert chosen is not None
        assert chosen.staff.staff_id == "B"

    def test_tier2_weighted_score_dataframe(self):
        rows = [
            {"staff_id": "A", "eta": 1.0, "fatigue": 10.0},
            {"staff_id": "B", "eta": 2.0, "fatigue": 5.0},
            {"staff_id": "C", "eta": 0.5, "fatigue": 40.0},
        ]
        df = pd.DataFrame(rows)
        df["score"] = (
            4 * (df["eta"] / ETA_SCALE) + 1 * (df["fatigue"] / FATIGUE_SCALE)
        )
        candidates = [
            Candidate(_staff(r.staff_id), eta=r.eta, fatigue=r.fatigue)
            for r in df.itertuples()
        ]
        chosen = select_candidate(candidates, tier=2)
        expected_id = df.loc[df["score"].idxmin(), "staff_id"]
        assert chosen is not None
        assert chosen.staff.staff_id == expected_id

    def test_tier3_fatigue_weight_dominates(self):
        rows = [
            {"staff_id": "A", "eta": 0.5, "fatigue": 45.0},
            {"staff_id": "B", "eta": 3.0, "fatigue": 5.0},
        ]
        df = pd.DataFrame(rows)
        df["score"] = (
            1 * (df["eta"] / ETA_SCALE) + 4 * (df["fatigue"] / FATIGUE_SCALE)
        )
        candidates = [
            Candidate(_staff(r.staff_id), eta=r.eta, fatigue=r.fatigue)
            for r in df.itertuples()
        ]
        chosen = select_candidate(candidates, tier=3)
        expected_id = df.loc[df["score"].idxmin(), "staff_id"]
        assert chosen is not None
        assert chosen.staff.staff_id == expected_id


class TestEngineIntegration:
    def setup_method(self):
        store.reset_simulation()

    def test_route_tier1_emergency_assigns_rn(self):
        result = engine.route_event_sequential(
            room="103",
            symptom_tags=["not breathing"],
            submitted_at=NOW,
        )
        assert result.tier == 1
        assert result.status == "assigned"
        assert result.assigned_staff_id is not None
        assert result.assigned_staff_id.startswith("RN")

    def test_route_tier3_routine(self):
        result = engine.route_event_sequential(
            room="104",
            symptom_tags=["needs bandage"],
            submitted_at=NOW,
        )
        assert result.tier == 3
        assert result.status == "assigned"

    def test_tier1_can_interrupt_busy_staff(self):
        # Tie up all RNs with tier 3 events
        store.reset_simulation()
        for room in ("101", "108"):
            engine.route_event_sequential(
                room=room,
                symptom_tags=["needs bandage"],
                submitted_at=NOW,
            )

        # Emergency should still assign (possibly interrupting)
        result = engine.route_event_sequential(
            room="105",
            symptom_tags=["unresponsive"],
            submitted_at=NOW + timedelta(seconds=1),
        )
        assert result.tier == 1
        assert result.status == "assigned"

    def test_clear_assignment_frees_staff(self):
        result = engine.route_event_sequential(
            room="102",
            symptom_tags=["possible fall"],
            submitted_at=NOW,
        )
        assert result.status == "assigned"
        staff = store.get_staff(result.assigned_staff_id)  # type: ignore[arg-type]
        assert staff is not None
        assert staff.status == "busy"

        engine.clear_assignment(result.event_id, NOW + timedelta(minutes=10))
        staff = store.get_staff(result.assigned_staff_id)  # type: ignore[arg-type]
        assert staff is not None
        assert staff.status == "available"
        assert len(staff.task_history) == 1

    def test_pending_event_is_saved_and_visible(self):
        # Occupy all staff with routine Tier 3 tasks so the next event must
        # be queued as pending rather than blocking the submit path.
        room_cycle = ["101", "102", "103", "104", "105", "106", "108"]
        for room in room_cycle:
            result = engine.route_event_sequential(
                room=room,
                symptom_tags=["needs bandage"],
                submitted_at=NOW,
            )
            assert result.status == "assigned"

        pending_result = engine.route_event_sequential(
            room="110",
            symptom_tags=["needs bandage"],
            submitted_at=NOW + timedelta(seconds=1),
        )
        assert pending_result.status == "pending"

        event = store.get_event(pending_result.event_id)
        assert event is not None
        assert event.status == "pending"

        all_pending = [e for e in store.all_events() if e.status == "pending"]
        assert any(e.event_id == pending_result.event_id for e in all_pending)

    def test_reset_simulation_restarts_backend_state(self):
        result = engine.route_event_sequential(
            room="102",
            symptom_tags=["possible fall"],
            submitted_at=NOW,
        )
        assert result.status == "assigned"

        engine.reset_simulation()

        assert store.get_event(result.event_id) is None
        assert store.pending_event_ids() == []

        # Reset should reseed staff but not leave any old assignment state.
        all_staff = store.all_staff()
        assert all(s.status == "available" for s in all_staff)
        assert all(s.current_event_id is None for s in all_staff)
