"""One-way compact V2 presentation from already completed V1 results."""

from typing import cast

from schemas.analysis import (
    AmbiguousResponse,
    CompletedResponse,
    NonCompletedResponse,
)
from schemas.public_rating_v2 import (
    PublicEvent,
    PublicGameIntelligence,
    PublicRatingStatus,
    PublicRatingV2Failure,
    PublicRatingV2Response,
    PublicRatingValue,
)
from services.analysis_rating_projection import (
    event_arbitration as event_arbitration,
)
from services.analysis_rating_projection import (
    game_intelligence_result,
)
from services.event_arbitration.models import ArbitratedEvent, ArbitrationResult, EventType
from services.player_rating.models import PlayerRatingSummary
from services.player_rating.models import PlayerRatingValue as InternalRatingValue

VERSION = "public_rating_v2"


def public_rating_v2(
    result: CompletedResponse | NonCompletedResponse | AmbiguousResponse,
    arbitration: ArbitrationResult | None = None,
) -> PublicRatingV2Response | PublicRatingV2Failure:
    if not isinstance(result, CompletedResponse):
        status = result.status
        warnings = result.warnings
        return PublicRatingV2Failure(
            request_id=result.analysis_id,
            analysis={"id": result.analysis_id, "status": status, "response_version": VERSION},
            metadata=result.metadata,
            reason=warnings[0] if warnings else "Analysis did not complete.",
            reason_code=status,
            warnings=warnings,
            retryable=status == "failed",
        )
    if result.result_availability == "UNAVAILABLE":
        raise ValueError("Unavailable completed responses cannot be projected as Public Rating V2.")
    selected_player = result.selected_player
    scores = result.scores
    if selected_player is None or scores is None:
        raise ValueError("Available completed responses require selected player and scores.")
    rating_summary = result.player_rating_summary
    if rating_summary is None:
        raise ValueError("Completed response is missing its PlayerRatingEngine summary.")
    ratings: dict[str, PublicRatingValue | PublicGameIntelligence] = {
        "technical_skill": _engine_rating(rating_summary, "technical_skill"),
        "physical_activity": _engine_rating(rating_summary, "physical_activity"),
        "ball_involvement": _engine_rating(rating_summary, "ball_involvement"),
    }
    resolved_arbitration = arbitration or event_arbitration(result)
    game_intelligence = game_intelligence_result(result, resolved_arbitration)
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
        "scalability",
    ):
        ratings[name] = _engine_rating(rating_summary, name)
    overall = _public_rating(rating_summary.overall)
    events = {
        "timeline": [
            _arbitrated_event(item, result.video.fps) for item in resolved_arbitration.events
        ]
    }
    return PublicRatingV2Response(
        request_id=result.analysis_id,
        analysis={
            "id": result.analysis_id,
            "status": result.status,
            "response_version": VERSION,
            "rating_version": rating_summary.version,
        },
        metadata=result.metadata,
        video={
            "duration_seconds": result.video.duration_seconds,
            "fps": result.video.fps,
            "resolution": {"width": result.video.width, "height": result.video.height},
        },
        player={
            "track_id": selected_player.track_id,
            "selection_confidence": selected_player.confidence,
            "visibility_ratio": selected_player.visibility_ratio,
            "visible_duration_seconds": selected_player.segment_duration_seconds
            or result.video.duration_seconds,
        },
        ratings=ratings,
        overall=overall,
        summary={
            "possible_ball_interactions": int(
                result.interaction_analysis.possible_ball_interaction_count.value or 0
            ),
            "controlled_movements": _arbitrated_count(resolved_arbitration, "controlled_movement"),
            "dribbles": _arbitrated_count(resolved_arbitration, "dribble"),
            "ball_losses": _arbitrated_count(resolved_arbitration, "ball_loss"),
            "passes": _arbitrated_count(resolved_arbitration, "pass"),
            "shots": _arbitrated_count(resolved_arbitration, "shot"),
            "ambiguous_events": resolved_arbitration.ambiguous_conflict_count,
            "deduplicated_total_events": resolved_arbitration.public_event_count,
        },
        quality={
            "tracking": {
                "visibility_ratio": selected_player.visibility_ratio,
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
            "rating": rating_summary.version,
            "analysis": result.analysis_version,
            "technical_events": result.algorithm_versions.get("controlled_movement", "unknown"),
            "event_arbitration": resolved_arbitration.version,
        },
    )


def _engine_rating(summary: PlayerRatingSummary, category: str) -> PublicRatingValue:
    rating = next(item for item in summary.categories if item.category == category)
    return _public_rating(rating)


def _public_rating(rating: InternalRatingValue) -> PublicRatingValue:
    return PublicRatingValue(
        value=rating.value,
        confidence=rating.confidence,
        status=cast(PublicRatingStatus, rating.status),
        level=rating.level,
        explanation=rating.explanation,
        reason=str(rating.evidence["reason"]) if rating.value is None else None,
        limitations=list(rating.limitations),
        version=rating.version,
    )


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
