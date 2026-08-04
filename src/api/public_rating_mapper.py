"""One-way compact V2 presentation from already completed V1 results."""

from schemas.analysis import (
    AmbiguousResponse,
    CompletedResponse,
    NonCompletedResponse,
    UnsupportedMetric,
)
from schemas.public_rating_v2 import (
    PublicEvent,
    PublicRatingV2Failure,
    PublicRatingV2Response,
    PublicRatingValue,
)

VERSION = "public_rating_v2"


def public_rating_v2(
    result: CompletedResponse | NonCompletedResponse | AmbiguousResponse,
) -> PublicRatingV2Response | PublicRatingV2Failure:
    if not isinstance(result, CompletedResponse):
        status = result.status
        warnings = result.warnings
        return PublicRatingV2Failure(
            analysis={"id": result.analysis_id, "status": status, "response_version": VERSION},
            reason=warnings[0] if warnings else "Analysis did not complete.",
            reason_code=status,
            warnings=warnings,
            retryable=status == "failed",
        )
    technical = result.scores.technical
    physical = result.scores.physical
    ratings = {
        "technical_skill": _score(technical, "technical_skill"),
        "physical_activity": _score(physical, "physical_activity"),
        "ball_involvement": PublicRatingValue(
            value=None,
            confidence=0,
            status="insufficient_evidence",
            reason="player_rating_summary_not_attached",
            limitations=["ball_proximity_does_not_prove_possession"],
            version="player_rating_v1",
        ),
    }
    for name in (
        "soccer_intelligence",
        "tactical_vision",
        "mental_stability",
        "professionalism",
        "growth_potential",
        "market_readiness",
    ):
        ratings[name] = PublicRatingValue(
            value=None,
            confidence=0,
            status="unsupported",
            reason="unsupported_by_current_pipeline",
            limitations=[],
            version="player_rating_v1",
        )
    available = [item for item in ratings.values() if item.value is not None]
    overall = PublicRatingValue(
        value=sum(item.value or 0 for item in available) / len(available)
        if len(available) >= 2
        else None,
        confidence=sum(item.confidence for item in available) / len(available)
        if len(available) >= 2
        else 0,
        status="available" if len(available) >= 2 else "insufficient_evidence",
        level=None,
        reason=None if len(available) >= 2 else "insufficient_supported_categories",
        limitations=["provisional_product_weights", "unavailable_categories_are_not_zero"],
        version="player_rating_v1",
    )
    events = {
        "controlled_movements": [
            _controlled(item)
            for item in result.technical_event_analysis.controlled_movement_candidates
        ],
        "dribbles": [_dribble(item) for item in result.technical_event_analysis.dribble_candidates],
        "ball_losses": [
            _loss(item) for item in result.technical_event_analysis.ball_loss_candidates
        ],
        "passes": [_pass(item) for item in result.pass_detection.pass_candidates],
        "shots": [_shot(item) for item in result.shot_detection.shot_candidates],
    }
    return PublicRatingV2Response(
        analysis={
            "id": result.analysis_id,
            "status": result.status,
            "response_version": VERSION,
            "rating_version": "player_rating_v1",
        },
        video={
            "duration_seconds": result.video.duration_seconds,
            "fps": result.video.fps,
            "resolution": {"width": result.video.width, "height": result.video.height},
        },
        player={
            "track_id": result.selected_player.track_id,
            "selection_confidence": result.selected_player.confidence,
            "visibility_ratio": result.selected_player.visibility_ratio,
            "visible_duration_seconds": result.selected_player.segment_duration_seconds
            or result.video.duration_seconds,
        },
        ratings=ratings,
        overall=overall,
        summary={
            "possible_ball_interactions": int(
                result.interaction_analysis.possible_ball_interaction_count.value or 0
            ),
            "controlled_movements": len(events["controlled_movements"]),
            "dribbles": len(events["dribbles"]),
            "ball_losses": len(events["ball_losses"]),
            "passes": len(events["passes"]),
            "shots": len(events["shots"]),
        },
        quality={
            "tracking": {
                "visibility_ratio": result.selected_player.visibility_ratio,
                "status": "good",
            },
            "movement": {
                "analysis_quality": float(result.diagnostics.movement_analysis_quality or 0),
                "status": "available",
            },
            "interaction": {
                "evidence_coverage_ratio": float(
                    result.interaction_analysis.interaction_evidence_coverage_ratio.value or 0
                ),
                "status": "available",
            },
            "technical_events": {
                "analysis_quality": float(result.diagnostics.technical_event_analysis_quality or 0),
                "status": "available",
            },
        },
        events=events,
        limitations=[
            "image_space_measurements",
            "ball_proximity_does_not_prove_possession",
            "candidate_events_are_not_confirmed_actions",
            "short_video_limits_generalization",
        ],
        warnings=result.warnings,
        versions={
            "response": VERSION,
            "rating": "player_rating_v1",
            "analysis": result.analysis_version,
            "technical_events": result.algorithm_versions.get("controlled_movement", "unknown"),
        },
    )


def _score(score: object, name: str) -> PublicRatingValue:
    if isinstance(score, UnsupportedMetric):
        return PublicRatingValue(
            value=None,
            confidence=0,
            status="insufficient_evidence",
            reason=score.reason,
            limitations=[],
            version="player_rating_v1",
        )
    return PublicRatingValue(
        value=score.value,
        confidence=score.confidence,
        status=score.status,
        level=None,
        explanation=None,
        limitations=[],
        version=score.version,
    )


def _event(
    item: object,
    event_type: str,
    start: float,
    end: float,
    duration: float,
    confidence: float,
    details: dict[str, float | int | str | bool | None],
) -> PublicEvent:
    identifier = (
        getattr(item, "event_id", None)
        or getattr(item, "pass_id", None)
        or getattr(item, "shot_id")
    )
    return PublicEvent(
        id=identifier,
        type=event_type,
        confidence=confidence,
        start_seconds=start,
        end_seconds=end,
        duration_seconds=duration,
        details=details,
    )


def _controlled(x: object) -> PublicEvent:
    return _event(
        x,
        "controlled_movement",
        x.start_time_seconds,
        x.end_time_seconds,
        x.duration_seconds,
        x.confidence,
        {
            "normalized_player_displacement": x.normalized_player_displacement,
            "direction_similarity": x.direction_similarity,
        },
    )


def _dribble(x: object) -> PublicEvent:
    return _event(
        x,
        "dribble",
        x.start_frame / 30,
        x.end_frame / 30,
        x.duration_seconds,
        x.confidence,
        {"subtype": x.candidate_subtype, "filtered_direction_changes": x.direction_changes},
    )


def _loss(x: object) -> PublicEvent:
    return _event(
        x,
        "ball_loss",
        x.event_time_seconds,
        x.event_time_seconds,
        0,
        x.confidence,
        {
            "recovery_detected": x.recovered_within_window,
            "post_evidence_seconds": x.post_evidence_frames / 30,
        },
    )


def _pass(x: object) -> PublicEvent:
    return _event(
        x,
        "pass",
        x.start_frame / 30,
        x.end_frame / 30,
        x.duration_seconds,
        x.confidence,
        {
            "release_seconds": x.release_frame / 30,
            "receiver_track_id": x.receiver_track_id,
            "distance_pixels": x.distance,
            "trajectory_quality": x.trajectory_quality,
        },
    )


def _shot(x: object) -> PublicEvent:
    return _event(
        x,
        "shot",
        x.start_frame / 30,
        x.end_frame / 30,
        x.duration_seconds,
        x.confidence,
        {
            "release_seconds": x.release_frame / 30,
            "trajectory_quality": x.trajectory_quality,
            "preparation_confidence": x.preparation_confidence,
            "follow_through_confidence": x.follow_through_confidence,
        },
    )
