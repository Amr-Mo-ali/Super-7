"""Red contract tests for Apex's additive target-result callback availability surface."""

from typing import Any

import pytest
from pydantic import ValidationError

from services.callback_service import CallbackPayload, DetailedRatings

_APPROVED_REASONS = (
    "ambiguous_visual_target",
    "no_qualifying_visual_target",
    "target_not_established",
)
_V2_PLAYER: dict[str, float | int] = {
    "track_id": 7,
    "selection_confidence": 0.91,
    "visibility_ratio": 0.72,
    "visible_duration_seconds": 8.4,
}
_OVERALL: dict[str, float | str] = {"value": 75.0, "confidence": 0.81, "status": "available"}


def _callback(
    *,
    resultAvailability: str | None = None,
    unavailabilityReason: str | None = None,
    player: dict[str, float | int] | None = None,
    overall: dict[str, float | str] | None = _OVERALL,
    overallConfidence: float | None = None,
) -> CallbackPayload:
    """Build the existing valid callback while specifying additive availability fields."""
    return CallbackPayload.model_validate(
        {
            "request_id": "request-789",
            "video_id": "video-123",
            "player_id": "player-456",
            "status": "COMPLETED",
            "summary": {"passes": 2},
            "ratings": {"technical_skill": {"value": 75.0}},
            "overall": overall,
            "detailed": DetailedRatings(),
            "events": {"timeline": []},
            "resultAvailability": resultAvailability,
            "unavailabilityReason": unavailabilityReason,
            "player": player,
            "overallConfidence": overallConfidence,
        }
    )


def _json(
    *,
    resultAvailability: str | None = None,
    unavailabilityReason: str | None = None,
    player: dict[str, float | int] | None = None,
    overall: dict[str, float | str] | None = _OVERALL,
    overallConfidence: float | None = None,
) -> dict[str, object]:
    return _callback(
        resultAvailability=resultAvailability,
        unavailabilityReason=unavailabilityReason,
        player=player,
        overall=overall,
        overallConfidence=overallConfidence,
    ).model_dump(mode="json", by_alias=True)


def test_available_callback_serializes_additive_target_result_fields() -> None:
    serialized = _json(
        resultAvailability="AVAILABLE",
        unavailabilityReason=None,
        player=_V2_PLAYER,
        overallConfidence=0.81,
    )
    assert serialized["status"] == "COMPLETED"
    assert serialized["resultAvailability"] == "AVAILABLE"
    assert serialized["unavailabilityReason"] is None
    assert serialized["player"] == _V2_PLAYER
    assert serialized["overall"] == _OVERALL
    assert serialized["overallConfidence"] == 0.81
    assert serialized["request_id"] == "request-789"
    assert serialized["ratings"] == {"technical_skill": {"value": 75.0}}


def test_each_approved_reason_serializes_a_completed_unavailable_callback() -> None:
    for reason in _APPROVED_REASONS:
        serialized = _json(
            resultAvailability="UNAVAILABLE",
            unavailabilityReason=reason,
            player=None,
            overall=None,
            overallConfidence=None,
        )
        assert serialized["status"] == "COMPLETED"
        assert serialized["resultAvailability"] == "UNAVAILABLE"
        assert serialized["unavailabilityReason"] == reason
        assert serialized["player"] is None
        assert serialized["overall"] is None
        assert serialized["overallConfidence"] is None


def test_unavailable_callback_is_completed_payload_not_failed_payload() -> None:
    payload = _callback(
        resultAvailability="UNAVAILABLE",
        unavailabilityReason="ambiguous_visual_target",
        player=None,
        overall=None,
        overallConfidence=None,
    )
    serialized = payload.model_dump(mode="json", by_alias=True)
    assert isinstance(payload, CallbackPayload)
    assert payload.error is None
    assert serialized["status"] == "COMPLETED"
    assert serialized["resultAvailability"] == "UNAVAILABLE"


def test_available_callback_rejects_a_missing_player() -> None:
    with pytest.raises(ValidationError):
        _callback(
            resultAvailability="AVAILABLE",
            unavailabilityReason=None,
            player=None,
            overallConfidence=0.81,
        )


def test_unavailable_callback_rejects_contradictory_target_result_values() -> None:
    contradictions = (
        {"player": _V2_PLAYER},
        {"overall": {"value": 75.0}},
        {"overallConfidence": 0.81},
        {"unavailabilityReason": None},
        {"unavailabilityReason": "unapproved_reason"},
    )
    for contradictory in contradictions:
        unavailable: dict[str, Any] = {
            "resultAvailability": "UNAVAILABLE",
            "unavailabilityReason": "ambiguous_visual_target",
            "player": None,
            "overall": None,
            "overallConfidence": None,
        }
        unavailable.update(contradictory)
        with pytest.raises(ValidationError):
            CallbackPayload.model_validate(unavailable)


def test_existing_callback_fields_remain_serialized_when_available_fields_are_supplied() -> None:
    serialized = _json(
        resultAvailability="AVAILABLE",
        unavailabilityReason=None,
        player=_V2_PLAYER,
        overallConfidence=0.81,
    )
    assert serialized["summary"] == {"passes": 2}
    assert serialized["detailed"] == {
        "speed_and_fitness": None,
        "ball_control_and_individual_skill": None,
        "passing_and_playmaking": None,
        "shooting_and_finishing": None,
        "defending_and_duels": None,
        "tactical_intelligence_and_teamwork": None,
        "positioning_and_off_ball_movement": None,
    }
    assert serialized["events"] == {"timeline": []}
    assert serialized["resultAvailability"] == "AVAILABLE"
