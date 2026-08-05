"""One-way compact V2 presentation from already completed V1 results."""

from schemas.analysis import (
    AmbiguousResponse,
    CompletedResponse,
    NonCompletedResponse,
    UnsupportedMetric,
)
from schemas.public_rating_v2 import (
    PublicEvent,
    PublicGameIntelligence,
    PublicRatingV2Failure,
    PublicRatingV2Response,
    PublicRatingValue,
)
from services.event_arbitration import EventArbitrator, EventCandidateRef
from services.player_rating.game_intelligence import (
    GameIntelligenceEngine,
    GameIntelligenceEvidence,
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
    arbitration = EventArbitrator().arbitrate(_event_candidates(result))
    game_intelligence = GameIntelligenceEngine().evaluate(_game_evidence(result, arbitration))
    ratings["game_intelligence"] = PublicGameIntelligence(
        value=game_intelligence.value,
        confidence=game_intelligence.confidence,
        status=game_intelligence.status,
        level=game_intelligence.level,
        reason=game_intelligence.reason,
        version=game_intelligence.version,
        components={
            item.name: PublicRatingValue(
                value=item.value,
                confidence=item.confidence,
                status=item.status,
                level=None,
                explanation=None,
                reason=item.evidence.get("reason") if item.value is None else None,
                limitations=[],
                version=item.version,
            )
            for item in game_intelligence.components
        },
        effective_weights=game_intelligence.effective_weights,
        limitations=list(game_intelligence.limitations),
    )
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
    available = [
        item
        for item in ratings.values()
        if isinstance(item, PublicRatingValue) and item.value is not None
    ]
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
        "timeline": [_arbitrated_event(item, result.video.fps) for item in arbitration.events]
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
            "controlled_movements": _arbitrated_count(arbitration, "controlled_movement"),
            "dribbles": _arbitrated_count(arbitration, "dribble"),
            "ball_losses": _arbitrated_count(arbitration, "ball_loss"),
            "passes": _arbitrated_count(arbitration, "pass"),
            "shots": _arbitrated_count(arbitration, "shot"),
            "ambiguous_events": arbitration.ambiguous_conflict_count,
            "deduplicated_total_events": arbitration.public_event_count,
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
            "event_arbitration": arbitration.version,
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


def _game_evidence(result: CompletedResponse, arbitration: object) -> GameIntelligenceEvidence:
    physical = result.scores.physical
    physical_evidence = getattr(physical, "evidence", None)
    technical = result.scores.technical
    interaction = result.interaction_analysis
    events = result.technical_event_analysis
    arbitrated = arbitration.events
    passes = [item for item in arbitrated if item.event_type == "pass"]
    shots = [item for item in arbitrated if item.event_type == "shot"]
    return GameIntelligenceEvidence(
        visible_duration_seconds=result.selected_player.segment_duration_seconds
        or result.video.duration_seconds,
        visibility_ratio=result.selected_player.visibility_ratio,
        continuity_ratio=(
            result.tracking.longest_continuous_visible_segment
            / max(result.selected_player.visible_frames, 1)
        ),
        ball_proximity_ratio=result.selected_player.ball_proximity_ratio,
        interaction_time_seconds=interaction.possible_ball_interaction_time_seconds.value,
        interaction_count=int(interaction.possible_ball_interaction_count.value or 0),
        longest_interaction_seconds=interaction.longest_possible_ball_interaction_seconds.value,
        interaction_confidence=interaction.mean_possible_ball_interaction_confidence.value,
        interaction_coverage=interaction.interaction_evidence_coverage_ratio.value,
        ball_quality=result.diagnostics.ball_analysis_quality,
        interaction_quality=result.diagnostics.interaction_analysis_quality,
        movement_intensity=physical_evidence.movement_intensity if physical_evidence else None,
        active_time_ratio=physical_evidence.active_time_ratio if physical_evidence else None,
        direction_component=physical_evidence.direction_component if physical_evidence else None,
        direction_changes=result.features.direction_changes.value,
        movement_quality=physical_evidence.movement_analysis_quality if physical_evidence else None,
        technical_quality=getattr(technical, "evidence", None) is not None
        and result.diagnostics.technical_event_analysis_quality
        or None,
        controlled_count=int(events.controlled_movement_candidate_count.value or 0),
        controlled_confidence=events.mean_controlled_movement_confidence.value,
        dribble_count=int(events.dribble_candidate_count.value or 0),
        dribble_confidence=events.mean_dribble_candidate_confidence.value,
        loss_count=int(events.ball_loss_candidate_count.value or 0),
        loss_confidence=events.mean_ball_loss_candidate_confidence.value,
        pass_count=len(passes),
        pass_confidence=_arbitrated_confidence(passes),
        shot_count=len(shots),
        shot_confidence=_arbitrated_confidence(shots),
        technical_value=getattr(technical, "value", None),
        technical_confidence=getattr(technical, "confidence", None),
        pass_shot_overlap_count=arbitration.ambiguous_conflict_count,
    )


def _candidate_confidence(candidates: list[object]) -> float | None:
    values = [getattr(candidate, "confidence", None) for candidate in candidates]
    usable = [value for value in values if isinstance(value, (int, float))]
    return sum(usable) / len(usable) if usable else None


def _event_candidates(result: CompletedResponse) -> tuple[EventCandidateRef, ...]:
    candidates: list[EventCandidateRef] = []
    for item in result.technical_event_analysis.controlled_movement_candidates:
        candidates.append(
            EventCandidateRef(
                item.event_id,
                "controlled_movement",
                item.start_frame,
                None,
                item.end_frame,
                result.selected_player.track_id,
                None,
                item.confidence,
                None,
                None,
                "controlled_movement",
            )
        )
    for item in result.technical_event_analysis.dribble_candidates:
        candidates.append(
            EventCandidateRef(
                item.event_id,
                "dribble",
                item.start_frame,
                None,
                item.end_frame,
                result.selected_player.track_id,
                None,
                item.confidence,
                None,
                None,
                "dribble",
            )
        )
    for item in result.technical_event_analysis.ball_loss_candidates:
        candidates.append(
            EventCandidateRef(
                item.event_id,
                "ball_loss",
                item.event_frame,
                None,
                item.event_frame,
                result.selected_player.track_id,
                None,
                item.confidence,
                None,
                None,
                "ball_loss",
            )
        )
    for item in result.pass_detection.pass_candidates:
        candidates.append(
            EventCandidateRef(
                item.pass_id,
                "pass",
                item.start_frame,
                item.release_frame,
                item.end_frame,
                item.possessor_track_id,
                item.receiver_track_id,
                item.confidence,
                item.trajectory_quality,
                item.distance,
                result.pass_detection.pass_detection_version,
            )
        )
    for item in result.shot_detection.shot_candidates:
        candidates.append(
            EventCandidateRef(
                item.shot_id,
                "shot",
                item.start_frame,
                item.release_frame,
                item.end_frame,
                item.possessor_track_id,
                None,
                item.confidence,
                item.trajectory_quality,
                item.distance,
                result.shot_detection.shot_detection_version,
                item.preparation_confidence,
                item.release_confidence,
                item.follow_through_confidence,
            )
        )
    return tuple(candidates)


def _arbitrated_event(item: object, fps: float) -> PublicEvent:
    return PublicEvent(
        id=item.public_event_id,
        type=item.event_type,
        status=item.status,
        confidence=item.event_confidence,
        arbitration_confidence=item.arbitration_confidence,
        start_seconds=item.start_frame / fps,
        release_seconds=item.release_frame / fps if item.release_frame is not None else None,
        end_seconds=item.end_frame / fps,
        duration_seconds=(item.end_frame - item.start_frame + 1) / fps,
        details={},
        candidate_types=list(item.candidate_types),
        source_candidate_ids=list(item.source_candidate_ids),
        limitations=list(item.limitations),
    )


def _arbitrated_count(arbitration: object, event_type: str) -> int:
    return sum(item.event_type == event_type for item in arbitration.events)


def _arbitrated_confidence(events: list[object]) -> float | None:
    return sum(item.event_confidence for item in events) / len(events) if events else None


def _event(
    item: object,
    event_type: str,
    start: float,
    end: float,
    duration: float,
    confidence: float,
    details: dict[str, float | int | str | bool | None],
) -> PublicEvent:
    identifier = getattr(item, "event_id", None) or getattr(item, "pass_id", None) or item.shot_id
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
