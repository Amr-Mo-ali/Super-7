"""Red contract for canonical callback aliases at the HTTP transport boundary."""

import asyncio
import json
import logging
from typing import Any, cast

import pytest
from pydantic import HttpUrl, TypeAdapter

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
from services.callback_service import (
    CallbackPayload,
    CallbackService,
    DetailedRatings,
    FailedCallbackPayload,
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
_ALIAS_KEYS = {"resultAvailability", "unavailabilityReason", "overallConfidence"}
_SNAKE_CASE_ALIAS_KEYS = {
    "result_availability",
    "unavailability_reason",
    "overall_confidence",
}
_CALLBACK_URL = TypeAdapter(HttpUrl).validate_python("https://backend.example.com/webhook")


def test_available_callback_uses_canonical_aliases_in_transport_bytes() -> None:
    wire = _send_and_capture(_available_callback())

    _assert_canonical_alias_keys(wire)
    assert wire["resultAvailability"] == "AVAILABLE"
    assert wire["unavailabilityReason"] is None
    assert wire["overallConfidence"] == 0.81
    assert wire["player"] == {
        "track_id": 7,
        "selection_confidence": 0.91,
        "visibility_ratio": 0.72,
        "visible_duration_seconds": 8.4,
    }
    assert wire["overall"] == {"value": 75.0, "confidence": 0.81, "status": "available"}
    assert wire["ratings"] == {"technical_skill": {"value": 75.0}}
    assert wire["detailed"] == _empty_detailed()
    assert wire["status"] == "COMPLETED"
    assert wire["summary"] == {"passes": 2}
    assert wire["events"] == {"timeline": []}
    assert wire["error"] is None


@pytest.mark.parametrize("reason", _APPROVED_REASONS)
def test_unavailable_callback_uses_canonical_aliases_without_fabricated_scores(
    reason: str,
) -> None:
    wire = _send_and_capture(_unavailable_callback(reason))

    _assert_canonical_alias_keys(wire)
    assert wire["status"] == "COMPLETED"
    assert wire["resultAvailability"] == "UNAVAILABLE"
    assert wire["unavailabilityReason"] == reason
    assert wire["player"] is None
    assert wire["overall"] is None
    assert wire["overallConfidence"] is None
    assert wire["ratings"] == {}
    assert wire["detailed"] == _empty_detailed()


def test_transport_preserves_numeric_zero_separately_from_explicit_null() -> None:
    available = _send_and_capture(_available_callback(overall_confidence=0.0))
    unavailable = _send_and_capture(_unavailable_callback("target_not_established"))

    _assert_canonical_alias_keys(available)
    _assert_canonical_alias_keys(unavailable)
    assert available["overallConfidence"] == 0.0
    assert isinstance(available["overallConfidence"], float)
    assert unavailable["overallConfidence"] is None


def test_legacy_success_fields_remain_unchanged_in_transport_bytes() -> None:
    payload = CallbackPayload(
        request_id="request-legacy-success",
        video_id="video-123",
        player_id="player-456",
        status="completed",
        summary={"passes": 2},
        ratings={"technical": {"value": 75}},
        overall={"value": 75.0},
        detailed=DetailedRatings(passing_and_playmaking=60.0),
        events={"timeline": []},
        error=None,
    )

    wire = _send_and_capture(payload)

    assert wire["status"] == "completed"
    assert wire["summary"] == {"passes": 2}
    assert wire["ratings"] == {"technical": {"value": 75}}
    assert wire["overall"] == {"value": 75.0}
    assert wire["detailed"]["passing_and_playmaking"] == 60.0
    assert wire["events"] == {"timeline": []}
    assert wire["error"] is None


def test_legacy_failure_fields_remain_unchanged_in_transport_bytes() -> None:
    payload = FailedCallbackPayload(
        request_id="request-legacy-failure",
        video_id="video-123",
        player_id="player-456",
        status="failed",
        summary={},
        ratings={},
        overall=None,
        events={},
        error={"code": "AnalysisFailed", "message": "Analysis could not be completed."},
    )

    wire = _send_and_capture(payload)

    assert wire["status"] == "failed"
    assert wire["summary"] == {}
    assert wire["ratings"] == {}
    assert wire["overall"] is None
    assert "detailed" not in wire
    assert wire["events"] == {}
    assert wire["error"] == {
        "code": "AnalysisFailed",
        "message": "Analysis could not be completed.",
    }


def test_available_mapper_payload_keeps_aliases_through_transport() -> None:
    payload = _callback_payload(_request(), _available_completed())

    wire = _send_and_capture(payload)

    _assert_canonical_alias_keys(wire)
    assert wire["resultAvailability"] == "AVAILABLE"
    assert wire["unavailabilityReason"] is None
    assert wire["player"] is not None
    assert wire["ratings"]
    assert wire["overall"] is not None
    assert wire["overallConfidence"] is not None


def test_unavailable_mapper_payload_keeps_aliases_through_transport() -> None:
    payload = _callback_payload(
        _request(),
        _unavailable_completed("no_qualifying_visual_target"),
    )

    wire = _send_and_capture(payload)

    _assert_canonical_alias_keys(wire)
    assert wire["status"] == "COMPLETED"
    assert wire["resultAvailability"] == "UNAVAILABLE"
    assert wire["unavailabilityReason"] == "no_qualifying_visual_target"
    assert wire["player"] is None
    assert wire["ratings"] == {}
    assert wire["overall"] is None
    assert wire["overallConfidence"] is None


def _send_and_capture(payload: CallbackPayload | FailedCallbackPayload) -> dict[str, Any]:
    bodies: list[bytes] = []

    def resolver(*_args: object, **_kwargs: object) -> list[tuple[Any, ...]]:
        return [(None, None, None, None, ("8.8.8.8", 443))]

    def transport(_url: str, body: bytes, _timeout: float) -> int:
        bodies.append(body)
        return 204

    async def no_sleep(_delay: float) -> None:
        raise AssertionError("successful callback transport must not retry")

    service = CallbackService(
        1,
        logging.getLogger("test.callback.transport.alias"),
        transport=transport,
        resolver=resolver,
        sleep=no_sleep,
    )
    assert asyncio.run(service.send_result(_CALLBACK_URL, payload)) is True
    assert len(bodies) == 1
    decoded: object = json.loads(bodies[0])
    assert isinstance(decoded, dict)
    return cast(dict[str, Any], decoded)


def _assert_canonical_alias_keys(wire: dict[str, Any]) -> None:
    keys = set(wire)
    assert {
        "missing_aliases": sorted(_ALIAS_KEYS - keys),
        "unexpected_snake_case": sorted(_SNAKE_CASE_ALIAS_KEYS & keys),
    } == {"missing_aliases": [], "unexpected_snake_case": []}


def _available_callback(*, overall_confidence: float = 0.81) -> CallbackPayload:
    return CallbackPayload(
        request_id="request-available",
        video_id="video-123",
        player_id="player-456",
        status="COMPLETED",
        summary={"passes": 2},
        ratings={"technical_skill": {"value": 75.0}},
        overall={
            "value": 75.0,
            "confidence": overall_confidence,
            "status": "available",
        },
        detailed=DetailedRatings(),
        events={"timeline": []},
        error=None,
        resultAvailability="AVAILABLE",
        unavailabilityReason=None,
        player={
            "track_id": 7,
            "selection_confidence": 0.91,
            "visibility_ratio": 0.72,
            "visible_duration_seconds": 8.4,
        },
        overallConfidence=overall_confidence,
    )


def _unavailable_callback(reason: str) -> CallbackPayload:
    return CallbackPayload.model_validate(
        {
            "request_id": "request-unavailable",
            "video_id": "video-123",
            "player_id": "player-456",
            "status": "COMPLETED",
            "summary": {},
            "ratings": {},
            "overall": None,
            "detailed": DetailedRatings(),
            "events": {},
            "error": None,
            "resultAvailability": "UNAVAILABLE",
            "unavailabilityReason": reason,
            "player": None,
            "overallConfidence": None,
        }
    )


def _request() -> AnalyzeRequest:
    return AnalyzeRequest.model_validate(
        {
            "videoId": "video-1",
            "playerId": "player-1",
            "videoUrl": "video.mp4",
            "callbackUrl": "https://backend.example.com/webhook",
        }
    )


def _available_completed() -> CompletedResponse:
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
            "interaction_v1",
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


def _unavailable_completed(reason: str) -> CompletedResponse:
    raw = _available_completed().model_dump(mode="json")
    raw.update(
        result_availability="UNAVAILABLE",
        unavailability_reason=reason,
        selected_player=None,
        scores=None,
        player_rating_summary=None,
    )
    return CompletedResponse.model_validate(raw)


def _empty_detailed() -> dict[str, None]:
    return {
        "speed_and_fitness": None,
        "ball_control_and_individual_skill": None,
        "passing_and_playmaking": None,
        "shooting_and_finishing": None,
        "defending_and_duels": None,
        "tactical_intelligence_and_teamwork": None,
        "positioning_and_off_ball_movement": None,
    }
