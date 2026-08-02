"""Deterministic tests for provisional physical video scoring."""

import pytest

from core.config import Settings
from core.exceptions import PhysicalScoreConfigurationError
from services.movement.schemas import MovementMetrics, MovementPoint, MovementResult
from services.scoring.level_mapper import ScoreLevelMapper
from services.scoring.physical_activity import RuleBasedPhysicalActivityScorer


def _movement(duration: float = 5, observations: int = 50) -> MovementResult:
    metrics = MovementMetrics(100, 20, 30, 0, 0, 2, 0, 0, 0, 0, 0.7, 0, 0, 0, 0, 0, 0, 0)
    points = tuple(
        MovementPoint(index, index * duration / (observations - 1), (0, 0), 10)
        for index in range(observations)
    )
    return MovementResult(metrics, points, 1, 0, observations)


def test_score_is_provisional_and_capped_for_raw_image_space() -> None:
    result = RuleBasedPhysicalActivityScorer(Settings()).score(
        _movement(), 0.8, 50, 50, 0.9, 0.8, "raw_image_space"
    )
    assert result.status == "provisional_video_based"
    assert result.value is not None and 0 <= result.value <= 100
    assert result.confidence is not None and result.confidence <= 0.75


def test_quality_gate_returns_null_not_zero() -> None:
    result = RuleBasedPhysicalActivityScorer(Settings()).score(
        _movement(), 0.1, 50, 50, 0.9, 0.8, "raw_image_space"
    )
    assert result.status == "insufficient_evidence" and result.value is None


def test_level_ties_choose_lower_level_and_configuration_is_validated() -> None:
    assert ScoreLevelMapper().map(60)[0] == 2
    with pytest.raises(PhysicalScoreConfigurationError):
        RuleBasedPhysicalActivityScorer(Settings(physical_score_activity_weight=0.9)).score(
            _movement(), 0.8, 50, 50, 0.9, 0.8, "raw_image_space"
        )
