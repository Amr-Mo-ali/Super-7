"""Contract tests for PlayerRatingEngine-backed public ratings."""

import pytest

from api.public_rating_mapper import public_rating_v2
from api.routes import _callback_payload
from schemas.analysis import (
    AnalyzeRequest,
    CompletedResponse,
    Diagnostics,
    FeatureMetric,
    FeaturesResponse,
    ScoresResponse,
    SelectedPlayer,
    TrackingResponse,
    UnsupportedMetric,
    VideoResponse,
)
from schemas.public_rating_v2 import PublicRatingV2Response
from services.interactions.models import InteractionAnalysisResult, InteractionDiagnostics
from services.player_rating.engine import PlayerRatingEngine
from services.player_rating.models import PlayerRatingSummary
from services.scoring.models import PhysicalScoreEvidence, PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult


def _technical(value: float | None = 80.0) -> TechnicalScoreResult:
    return TechnicalScoreResult(
        value, 0.8 if value is not None else None, "provisional", None, {}, None, None, 0, 0.9
    )


def _physical(value: float | None = 70.0) -> PhysicalScoreResult:
    evidence = PhysicalScoreEvidence(0.7, 0.8, 0.9, 0.8, 0.4, 0.9, 6.0, 60, 0.9)
    return PhysicalScoreResult(
        value,
        None,
        None,
        None,
        0.7 if value is not None else None,
        "provisional",
        "physical_v1",
        None,
        evidence if value is not None else None,
        (),
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


def _summary(
    technical: TechnicalScoreResult | None = None,
    physical: PhysicalScoreResult | None = None,
    interactions: InteractionAnalysisResult | None = None,
) -> PlayerRatingSummary:
    return PlayerRatingEngine().summarize(
        technical or _technical(), physical or _physical(), interactions or _interactions(), None
    )


def _completed(summary: PlayerRatingSummary) -> CompletedResponse:
    unavailable = UnsupportedMetric(reason="unavailable")
    return CompletedResponse.model_construct(
        analysis_id="analysis-1",
        status="completed",
        video=VideoResponse(duration_seconds=10, fps=10, width=64, height=64),
        selected_player=SelectedPlayer(
            track_id=1,
            selection_method="test",
            selection_score=1,
            confidence=0.9,
            visible_frames=100,
            visibility_ratio=1,
            ball_proximity_frames=0,
            ball_proximity_ratio=0,
            visibility_contribution=1,
            ball_proximity_contribution=0,
        ),
        tracking=TrackingResponse(
            frames_processed=100,
            lost_track_count=0,
            longest_continuous_visible_segment=100,
        ),
        features=FeaturesResponse(
            ball_proximity_time_seconds=FeatureMetric(),
            movement_intensity=FeatureMetric(),
            direction_changes=FeatureMetric(),
        ),
        scores=ScoresResponse(
            technical=unavailable,
            physical=unavailable,
            game_intelligence=unavailable,
            mental_resilience=unavailable,
            professionalism=unavailable,
            growth_potential=unavailable,
            market_readiness=unavailable,
        ),
        player_rating_summary=summary,
        diagnostics=Diagnostics(
            frames_processed=1,
            frames_with_player_detections=1,
            total_person_detections=1,
            tracks_created=1,
            valid_candidate_tracks=1,
            ball_detections=0,
        ),
        warnings=[],
        analysis_version="test",
        model_version="test",
        processing_time_ms=0,
    )


def test_public_ratings_use_engine_ball_involvement_and_weighted_overall() -> None:
    summary = _summary()
    response = public_rating_v2(_completed(summary))
    assert isinstance(response, PublicRatingV2Response)

    assert response.ratings["ball_involvement"].value == 60.0
    assert response.overall.value == pytest.approx(summary.overall.value)
    assert response.overall.value != pytest.approx((80 + 70 + 60) / 3)


def test_public_ratings_preserve_engine_evidence_gates_and_unsupported_categories() -> None:
    summary = _summary(interactions=_interactions(coverage=0.1))
    response = public_rating_v2(_completed(summary))
    assert isinstance(response, PublicRatingV2Response)

    ball = response.ratings["ball_involvement"]
    assert ball.value is None
    assert ball.status == "insufficient_evidence"
    assert ball.reason == "insufficient_interaction_evidence"
    for category in (
        "soccer_intelligence",
        "tactical_vision",
        "mental_stability",
        "professionalism",
        "growth_potential",
        "market_readiness",
        "scalability",
    ):
        rating = response.ratings[category]
        assert rating.value is None
        assert rating.status == "unsupported"
        assert rating.reason == "unsupported_by_current_pipeline"


def test_public_overall_stays_unavailable_when_engine_has_one_supported_category() -> None:
    summary = _summary(_technical(), _physical(None), _interactions(coverage=0.0))
    response = public_rating_v2(_completed(summary))
    assert isinstance(response, PublicRatingV2Response)

    assert response.overall.value is None
    assert response.overall.status == "insufficient_evidence"
    assert response.overall.reason == "insufficient_supported_categories"


def test_callback_payload_contains_engine_backed_ratings_and_overall() -> None:
    summary = _summary()
    payload = _callback_payload(
        AnalyzeRequest.model_validate(
            {
                "videoId": "video-1",
                "playerId": "player-1",
                "videoUrl": "video.mp4",
                "callbackUrl": "https://example.com/callback",
            }
        ),
        _completed(summary),
    )

    assert payload.ratings["ball_involvement"]["value"] == 60.0
    assert payload.ratings["technical_skill"]["value"] == 80.0
    assert payload.ratings["physical_activity"]["value"] == 70.0
    assert "game_intelligence" in payload.ratings
    assert payload.overall is not None
    assert payload.overall["value"] == pytest.approx(summary.overall.value)
    assert payload.overall["confidence"] == pytest.approx(summary.overall.confidence)
    assert payload.overall["status"] == "available"
    assert payload.detailed.model_dump(mode="json") == {
        "speed_and_fitness": None,
        "ball_control_and_individual_skill": None,
        "passing_and_playmaking": None,
        "shooting_and_finishing": None,
        "defending_and_duels": None,
        "tactical_intelligence_and_teamwork": None,
        "positioning_and_off_ball_movement": None,
    }
    assert set(payload.model_dump(mode="json")) == {
        "request_id",
        "video_id",
        "player_id",
        "status",
        "summary",
        "ratings",
        "overall",
        "detailed",
        "events",
        "error",
    }
    assert "scores" not in payload.model_dump(mode="json")
    assert "schema_version" not in payload.model_dump(mode="json")
    assert "overall" not in payload.detailed.model_dump(mode="json")


def test_callback_payload_preserves_unavailable_engine_overall() -> None:
    summary = _summary(_technical(), _physical(None), _interactions(coverage=0.0))
    payload = _callback_payload(
        AnalyzeRequest.model_validate(
            {
                "videoId": "video-1",
                "playerId": "player-1",
                "videoUrl": "video.mp4",
                "callbackUrl": "https://example.com/callback",
            }
        ),
        _completed(summary),
    )

    assert payload.overall is not None
    assert payload.overall["value"] is None
    assert payload.overall["status"] == "insufficient_evidence"
