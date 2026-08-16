"""Boundary coverage for the isolated Game Intelligence V0.1 heuristic."""

from dataclasses import replace
from typing import Any

import pytest

from services.player_rating.config import MAX_GAME_INTELLIGENCE_CONFIDENCE
from services.player_rating.game_intelligence import (
    GameIntelligenceEngine,
    GameIntelligenceEvidence,
)


def _evidence(**changes: Any) -> GameIntelligenceEvidence:
    value = GameIntelligenceEvidence(
        20,
        0.8,
        0.9,
        0.5,
        3,
        3,
        1.5,
        0.8,
        0.9,
        0.8,
        0.8,
        0.7,
        0.8,
        0.7,
        6,
        0.8,
        0.8,
        2,
        0.8,
        1,
        0.7,
        0,
        None,
        1,
        0.8,
        0,
        None,
        75,
        0.9,
    )
    return replace(value, **changes)


def test_all_components_available_and_weights_normalize() -> None:
    result = GameIntelligenceEngine().evaluate(_evidence())
    assert result.status == "provisional_video_based"
    assert result.available_component_count == 5
    assert sum(result.effective_weights.values()) == pytest.approx(1)
    assert result.value is not None and result.confidence <= MAX_GAME_INTELLIGENCE_CONFIDENCE
    assert result.evidence_gate is not None
    assert result.evidence_gate.failed_reasons == ()


def test_exactly_three_components_are_normalized_without_zero_filling() -> None:
    result = GameIntelligenceEngine().evaluate(
        _evidence(
            technical_value=None,
            technical_confidence=None,
            controlled_count=0,
            pass_count=0,
            shot_count=0,
        )
    )
    assert result.status == "provisional_video_based"
    assert result.available_component_count == 3
    assert "technical_involvement" not in result.effective_weights
    assert sum(result.effective_weights.values()) == pytest.approx(1)


def test_fewer_than_three_components_or_short_video_is_unavailable() -> None:
    engine = GameIntelligenceEngine()
    assert (
        engine.evaluate(
            _evidence(movement_quality=0, technical_value=None, technical_confidence=None)
        ).value
        is None
    )
    assert (
        engine.evaluate(_evidence(visible_duration_seconds=3.99)).reason
        == "insufficient_game_intelligence_evidence"
    )


def test_evidence_diagnostics_report_duration_and_component_count_failures() -> None:
    result = GameIntelligenceEngine().evaluate(
        _evidence(
            visible_duration_seconds=4,
            movement_quality=0,
            technical_value=None,
            technical_confidence=None,
            controlled_count=0,
            pass_count=0,
            shot_count=0,
        )
    )
    assert result.evidence_gate is not None
    assert result.evidence_gate.failed_reasons == ("available_component_count",)
    assert result.evidence_gate.component_failed_reasons == {
        "decision_consistency": "insufficient_technical_event_evidence",
        "spatial_activity_proxy": "insufficient_movement_evidence",
        "movement_efficiency_proxy": "insufficient_movement_evidence",
        "technical_involvement": "insufficient_technical_evidence",
    }


def test_score_and_confidence_are_separate_and_confidence_cap_applies() -> None:
    high_score_low_confidence = GameIntelligenceEngine().evaluate(
        _evidence(
            visible_duration_seconds=4,
            technical_value=100,
            technical_confidence=0.1,
            interaction_confidence=0.1,
        )
    )
    assert (
        high_score_low_confidence.value is not None and high_score_low_confidence.confidence < 0.2
    )
    low_score = GameIntelligenceEngine().evaluate(
        _evidence(
            movement_intensity=0,
            active_time_ratio=0,
            direction_component=0,
            ball_proximity_ratio=0,
            technical_value=5,
        )
    )
    assert low_score.value is not None and low_score.value < high_score_low_confidence.value


def test_ball_and_decision_gates_overlap_and_non_finite_are_safe() -> None:
    engine = GameIntelligenceEngine()
    result = engine.evaluate(_evidence(ball_quality=0.44, pass_shot_overlap_count=1))
    assert next(x for x in result.components if x.name == "ball_involvement").value is None
    decision = next(x for x in result.components if x.name == "decision_consistency")
    assert "pass_shot_candidate_overlap_not_arbitrated" in decision.limitations
    assert (
        engine.evaluate(
            _evidence(
                movement_intensity=float("nan"),
                ball_quality=0,
                technical_value=None,
                technical_confidence=None,
                controlled_count=0,
                pass_count=0,
                shot_count=0,
            )
        ).value
        is None
    )


def test_is_deterministic_and_uses_stable_levels_and_limitations() -> None:
    first = GameIntelligenceEngine().evaluate(_evidence())
    assert first == GameIntelligenceEngine().evaluate(_evidence())
    assert first.level == "good"
    assert "missing_team_context" in first.limitations
