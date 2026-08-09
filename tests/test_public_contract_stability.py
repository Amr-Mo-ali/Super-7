"""Serialization compatibility for V1 and compact Public Rating V2 contracts."""

from typing import cast

import pytest
from pydantic import ValidationError

from api.public_rating_mapper import public_rating_v2
from schemas.analysis import (
    Diagnostics,
    FeatureMetric,
    NonCompletedResponse,
    TechnicalScoreResponse,
    UnsupportedMetric,
)
from schemas.public_rating_v2 import (
    PublicEvent,
    PublicGameIntelligence,
    PublicRatingStatus,
    PublicRatingV2Failure,
    PublicRatingValue,
)


def test_v1_supported_and_unsupported_states_serialize_distinctly() -> None:
    supported = TechnicalScoreResponse(
        value=70,
        confidence=0.8,
        status="provisional_event_based",
        version="technical_scoring_v0.1",
        evidence={"dribble_events": 1},
    )
    unsupported = UnsupportedMetric(reason="upstream_unavailable")
    assert supported.model_dump(mode="json")["value"] == 70
    assert unsupported.model_dump(mode="json") == {"value": None, "reason": "upstream_unavailable"}
    assert FeatureMetric(value=0, reason=None).model_dump() == {"value": 0, "reason": None}


def test_v2_rating_states_keep_zero_and_insufficient_evidence_distinct() -> None:
    zero = PublicRatingValue(
        value=0,
        confidence=1,
        status="available",
        version="v",
    )
    insufficient = PublicRatingValue(
        value=None,
        confidence=0,
        status="insufficient_evidence",
        reason="missing_evidence",
        version="v",
    )
    assert zero.model_dump(mode="json")["value"] == 0
    assert insufficient.model_dump(mode="json")["value"] is None
    with pytest.raises(ValidationError):
        PublicRatingValue(
            value=1,
            confidence=0.5,
            status=cast(PublicRatingStatus, "unknown"),
            version="v",
        )


def test_v2_ambiguous_timeline_event_has_stable_deterministic_serialization() -> None:
    event = PublicEvent(
        id="conflict-1",
        type=None,
        status="ambiguous",
        confidence=0.54,
        arbitration_confidence=0.2,
        start_seconds=23.366,
        release_seconds=23.666,
        end_seconds=24.3,
        duration_seconds=0.934,
        candidate_types=["pass", "shot"],
        source_candidate_ids=["pass-1", "shot-1"],
        limitations=["pass_shot_conflict"],
    )
    assert event.model_dump_json() == event.model_dump_json()
    assert event.model_dump(mode="json")["type"] is None
    assert event.model_dump(mode="json")["status"] == "ambiguous"


def test_v2_game_intelligence_components_preserve_unavailable_contract() -> None:
    unavailable = PublicRatingValue(
        value=None,
        confidence=0,
        status="insufficient_evidence",
        reason="insufficient_technical_event_evidence",
        version="game_intelligence_v0.1",
    )
    game = PublicGameIntelligence(
        value=None,
        confidence=0,
        status="insufficient_evidence",
        reason="insufficient_game_intelligence_evidence",
        version="game_intelligence_v0.1",
        components={"decision_consistency": unavailable},
        effective_weights={},
        limitations=["heuristic_estimation"],
    )
    assert game.model_dump(mode="json")["components"]["decision_consistency"]["value"] is None


def test_internal_v1_noncompleted_response_maps_to_public_v2_failure() -> None:
    internal_result = NonCompletedResponse(
        analysis_id="analysis-1",
        status="no_valid_tracks",
        warnings=["No track passed the configured quality thresholds."],
        diagnostics=Diagnostics(
            frames_processed=10,
            frames_with_player_detections=2,
            total_person_detections=2,
            tracks_created=1,
            valid_candidate_tracks=0,
            ball_detections=0,
        ),
    )
    response = public_rating_v2(internal_result)
    assert response.analysis == {
        "id": "analysis-1",
        "status": "no_valid_tracks",
        "response_version": "public_rating_v2",
    }
    assert isinstance(response, PublicRatingV2Failure)
    assert response.reason_code == "no_valid_tracks"
