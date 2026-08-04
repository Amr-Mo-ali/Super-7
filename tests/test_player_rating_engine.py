"""Deterministic tests for the internal evidence-first Player Rating V1 layer."""

from math import nan

import pytest

from services.interactions.models import InteractionAnalysisResult, InteractionDiagnostics
from services.player_rating.engine import PlayerRatingEngine
from services.player_rating.models import PlayerRatingSummary
from services.scoring.models import PhysicalScoreEvidence, PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult


def _technical(value: float | None = 80.0, confidence: float | None = 0.8) -> TechnicalScoreResult:
    return TechnicalScoreResult(
        value,
        confidence,
        "provisional_event_based",
        None,
        {"controlled_movement_events": 2.0},
        None,
        None,
        0.0,
        0.9,
    )


def _physical(value: float | None = 70.0, confidence: float | None = 0.7) -> PhysicalScoreResult:
    evidence = PhysicalScoreEvidence(0.7, 0.8, 0.9, 0.8, 0.4, 0.9, 6.0, 60, 0.9)
    return PhysicalScoreResult(
        value,
        None,
        None,
        None,
        confidence,
        "provisional_video_based",
        "v",
        None,
        evidence if value is not None else None,
        ("image_space_measurements",),
        "visible activity",
        value,
        False,
        0,
    )


def _interactions(coverage: float = 0.9, count: int = 2) -> InteractionAnalysisResult:
    diagnostics = InteractionDiagnostics(1, 1, 0, 0, 1, 1, 0, 0, 0, 0, coverage, "v", 0.9, 0)
    return InteractionAnalysisResult(
        (), count, 3.0, 3.0, 0.9, 2, 2, coverage, "v", diagnostics, (), None
    )


def _summary() -> PlayerRatingSummary:
    return PlayerRatingEngine().summarize(_technical(), _physical(), _interactions(), None)


def test_supported_categories_and_overall_use_only_available_evidence() -> None:
    summary = _summary()
    assert summary.available_category_count == 3
    assert summary.overall.status == "available"
    assert summary.overall.evidence["categories_used"] == (
        "technical_skill",
        "physical_activity",
        "ball_involvement",
    )


@pytest.mark.parametrize(
    ("technical", "physical", "interactions", "category", "reason"),
    [
        (
            _technical(None, None),
            _physical(),
            _interactions(),
            "technical_skill",
            "insufficient_event_evidence",
        ),
        (
            _technical(),
            _physical(None, None),
            _interactions(),
            "physical_activity",
            "insufficient_movement_evidence",
        ),
        (
            _technical(),
            _physical(),
            _interactions(coverage=0.59),
            "ball_involvement",
            "insufficient_interaction_evidence",
        ),
    ],
)
def test_evidence_gates_do_not_create_numeric_values(
    technical: TechnicalScoreResult,
    physical: PhysicalScoreResult,
    interactions: InteractionAnalysisResult,
    category: str,
    reason: str,
) -> None:
    result = PlayerRatingEngine().summarize(technical, physical, interactions, None)
    item = next(item for item in result.categories if item.category == category)
    assert (
        item.value is None
        and item.status == "insufficient_evidence"
        and item.evidence["reason"] == reason
    )


def test_unsupported_categories_are_never_numeric() -> None:
    unsupported = [item for item in _summary().categories if item.status == "unsupported"]
    assert len(unsupported) == 7
    assert all(
        item.value is None and item.evidence["reason"] == "unsupported_by_current_pipeline"
        for item in unsupported
    )


def test_overall_is_unavailable_with_fewer_than_two_categories() -> None:
    summary = PlayerRatingEngine().summarize(
        _technical(), _physical(None, None), _interactions(coverage=0.0), None
    )
    assert (
        summary.overall.value is None
        and summary.overall.evidence["reason"] == "insufficient_supported_categories"
    )


def test_score_confidence_separation_clamping_and_finiteness() -> None:
    technical = (
        PlayerRatingEngine()
        .summarize(_technical(99.0, 0.1), _physical(), _interactions(), None)
        .categories[0]
    )
    assert technical.value == 99.0 and technical.confidence == 0.1
    clamped = (
        PlayerRatingEngine()
        .summarize(_technical(200.0, nan), _physical(), _interactions(), None)
        .categories[0]
    )
    assert clamped.value == 100.0 and clamped.confidence == 0.0


@pytest.mark.parametrize(
    ("value", "label"),
    [
        (0, "very_low"),
        (20, "low"),
        (35, "developing"),
        (50, "moderate"),
        (65, "good"),
        (80, "very_good"),
        (90, "excellent"),
    ],
)
def test_level_boundaries(value: float, label: str) -> None:
    assert PlayerRatingEngine()._level(value) == label


def test_limitations_versions_and_determinism_are_stable() -> None:
    first, second = _summary(), _summary()
    ball = next(item for item in first.categories if item.category == "ball_involvement")
    physical = next(item for item in first.categories if item.category == "physical_activity")
    assert first == second and first.version == "player_rating_v1"
    assert "ball_proximity_does_not_prove_possession" in ball.limitations
    assert "physical fitness" not in physical.explanation.lower()
