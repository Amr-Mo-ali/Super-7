"""Red contract for successful-but-unavailable internal analysis results."""

import pytest
from pydantic import ValidationError

from api.routes import _callback_payload
from schemas.analysis import (
    AnalyzeRequest,
    CompletedResponse,
    Diagnostics,
    FeatureMetric,
    FeaturesResponse,
    NonCompletedResponse,
    ScoresResponse,
    SelectedPlayer,
    TrackingResponse,
    UnsupportedMetric,
    VideoResponse,
)
from services.interactions.models import InteractionAnalysisResult, InteractionDiagnostics
from services.player_rating.engine import PlayerRatingEngine
from services.scoring.models import PhysicalScoreEvidence, PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult

_APPROVED_REASONS = (
    "ambiguous_visual_target",
    "no_qualifying_visual_target",
    "target_not_established",
)


def _available() -> CompletedResponse:
    summary = PlayerRatingEngine().summarize(
        TechnicalScoreResult(80.0, 0.8, "provisional", None, {}, None, None, 0, 0.9),
        PhysicalScoreResult(
            70.0,
            None,
            None,
            None,
            0.7,
            "provisional",
            "physical_v1",
            None,
            PhysicalScoreEvidence(0.7, 0.8, 0.9, 0.8, 0.4, 0.9, 6.0, 60, 0.9),
            (),
            "visible activity",
            70.0,
            False,
            0,
        ),
        InteractionAnalysisResult(
            (),
            2,
            3.0,
            3.0,
            0.9,
            2,
            2,
            0.9,
            "v",
            InteractionDiagnostics(1, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0.9, "v", 0.9, 0),
            (),
            None,
        ),
        None,
    )
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


def _unavailable(reason: str) -> CompletedResponse:
    raw = _available().model_dump(mode="json")
    raw.update(
        result_availability="UNAVAILABLE",
        unavailability_reason=reason,
        selected_player=None,
        scores=None,
        player_rating_summary=None,
    )
    return CompletedResponse.model_validate(raw)


def _request() -> AnalyzeRequest:
    return AnalyzeRequest.model_validate(
        {
            "videoId": "video-1",
            "playerId": "player-1",
            "videoUrl": "video.mp4",
            "callbackUrl": "https://example.com/callback",
        }
    )


def test_available_completed_response_preserves_player_ratings_and_confidence() -> None:
    result = _available()
    assert isinstance(result, CompletedResponse)
    assert result.selected_player.track_id == 1
    assert result.player_rating_summary is not None
    assert result.player_rating_summary.overall.confidence is not None


def test_each_approved_reason_constructs_a_completed_unavailable_result() -> None:
    for reason in _APPROVED_REASONS:
        result = _unavailable(reason)
        assert result.status == "completed"
        assert result.result_availability == "UNAVAILABLE"
        assert result.unavailability_reason == reason
        assert result.selected_player is None
        assert result.scores is None
        assert result.player_rating_summary is None


def test_unavailable_internal_result_is_not_noncompleted_or_failed() -> None:
    result = _unavailable("ambiguous_visual_target")
    assert isinstance(result, CompletedResponse)
    assert not isinstance(result, NonCompletedResponse)
    assert result.status == "completed"
    assert not hasattr(result, "error")


def test_unavailable_internal_result_rejects_contradictory_player_and_rating_data() -> None:
    unavailable = _unavailable("ambiguous_visual_target")
    contradictions = (
        {"selected_player": _available().selected_player},
        {"scores": _available().scores},
        {"player_rating_summary": _available().player_rating_summary},
        {"unavailability_reason": None},
        {"unavailability_reason": "unapproved_reason"},
    )
    for contradictory in contradictions:
        raw = unavailable.model_dump(mode="json")
        raw.update(contradictory)
        with pytest.raises(ValidationError):
            CompletedResponse.model_validate(raw)


def test_unavailable_internal_result_maps_to_the_approved_callback_shape() -> None:
    payload = _callback_payload(_request(), _unavailable("ambiguous_visual_target"))
    serialized = payload.model_dump(mode="json", by_alias=True)
    assert serialized["status"] == "COMPLETED"
    assert serialized["resultAvailability"] == "UNAVAILABLE"
    assert serialized["unavailabilityReason"] == "ambiguous_visual_target"
    assert serialized["player"] is None
    assert serialized["overall"] is None
    assert serialized["overallConfidence"] is None
