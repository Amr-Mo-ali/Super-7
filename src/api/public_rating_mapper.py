"""One-way compact V2 presentation from already completed V1 results."""

from typing import cast

from schemas.analysis import (
    AmbiguousResponse,
    CompletedResponse,
    NonCompletedResponse,
    PhysicalScoreResponse,
    TechnicalScoreResponse,
    UnsupportedMetric,
)
from schemas.public_rating_v2 import (
    PublicEvent,
    PublicGameIntelligence,
    PublicRatingStatus,
    PublicRatingV2Failure,
    PublicRatingV2Response,
    PublicRatingValue,
)
from services.event_arbitration import EventArbitrator, EventCandidateRef
from services.event_arbitration.models import ArbitratedEvent, ArbitrationResult, EventType
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
            metadata=result.metadata,
            reason=warnings[0] if warnings else "Analysis did not complete.",
            reason_code=status,
            warnings=warnings,
            retryable=status == "failed",
        )
    technical = result.scores.technical
    physical = result.scores.physical
    ratings: dict[str, PublicRatingValue | PublicGameIntelligence] = {
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
        status=cast(PublicRatingStatus, game_intelligence.status),
        level=game_intelligence.level,
        reason=game_intelligence.reason,
        version=game_intelligence.version,
        components={
            item.name: PublicRatingValue(
                value=item.value,
                confidence=item.confidence,
                status=cast(PublicRatingStatus, item.status),
                level=None,
                explanation=None,
                reason=(str(item.evidence.get("reason")) if item.value is None else None),
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
    available: list[PublicRatingValue] = [
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
        metadata=result.metadata,
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


def _score(
    score: TechnicalScoreResponse | PhysicalScoreResponse | UnsupportedMetric,
    name: str,
) -> PublicRatingValue:
    del name
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
        confidence=score.confidence or 0.0,
        status=cast(PublicRatingStatus, score.status),
        level=None,
        explanation=None,
        limitations=[],
        version=score.version,
    )


def _game_evidence(
    result: CompletedResponse, arbitration: ArbitrationResult
) -> GameIntelligenceEvidence:
    physical = result.scores.physical
    physical_evidence = physical.evidence if isinstance(physical, PhysicalScoreResponse) else None
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
        technical_quality=(
            result.diagnostics.technical_event_analysis_quality
            if isinstance(technical, TechnicalScoreResponse)
            else None
        ),
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
        technical_value=technical.value if isinstance(technical, TechnicalScoreResponse) else None,
        technical_confidence=(
            technical.confidence if isinstance(technical, TechnicalScoreResponse) else None
        ),
        pass_shot_overlap_count=arbitration.ambiguous_conflict_count,
    )


def _event_candidates(result: CompletedResponse) -> tuple[EventCandidateRef, ...]:
    candidates: list[EventCandidateRef] = []
    for controlled in result.technical_event_analysis.controlled_movement_candidates:
        candidates.append(
            EventCandidateRef(
                controlled.event_id,
                "controlled_movement",
                controlled.start_frame,
                None,
                controlled.end_frame,
                result.selected_player.track_id,
                None,
                controlled.confidence,
                None,
                None,
                "controlled_movement",
            )
        )
    for dribble in result.technical_event_analysis.dribble_candidates:
        candidates.append(
            EventCandidateRef(
                dribble.event_id,
                "dribble",
                dribble.start_frame,
                None,
                dribble.end_frame,
                result.selected_player.track_id,
                None,
                dribble.confidence,
                None,
                None,
                "dribble",
            )
        )
    for loss in result.technical_event_analysis.ball_loss_candidates:
        candidates.append(
            EventCandidateRef(
                loss.event_id,
                "ball_loss",
                loss.event_frame,
                None,
                loss.event_frame,
                result.selected_player.track_id,
                None,
                loss.confidence,
                None,
                None,
                "ball_loss",
            )
        )
    for passing in result.pass_detection.pass_candidates:
        candidates.append(
            EventCandidateRef(
                passing.pass_id,
                "pass",
                passing.start_frame,
                passing.release_frame,
                passing.end_frame,
                passing.possessor_track_id,
                passing.receiver_track_id,
                passing.confidence,
                passing.trajectory_quality,
                passing.distance,
                result.pass_detection.pass_detection_version,
            )
        )
    for shot in result.shot_detection.shot_candidates:
        candidates.append(
            EventCandidateRef(
                shot.shot_id,
                "shot",
                shot.start_frame,
                shot.release_frame,
                shot.end_frame,
                shot.possessor_track_id,
                None,
                shot.confidence,
                shot.trajectory_quality,
                shot.distance,
                result.shot_detection.shot_detection_version,
                shot.preparation_confidence,
                shot.release_confidence,
                shot.follow_through_confidence,
            )
        )
    return tuple(candidates)


def _arbitrated_event(item: ArbitratedEvent, fps: float) -> PublicEvent:
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


def _arbitrated_count(arbitration: ArbitrationResult, event_type: EventType) -> int:
    return sum(item.event_type == event_type for item in arbitration.events)


def _arbitrated_confidence(events: list[ArbitratedEvent]) -> float | None:
    return sum(item.event_confidence for item in events) / len(events) if events else None
