"""Shared event arbitration and game-intelligence evidence projection."""

from schemas.analysis import CompletedResponse, PhysicalScoreResponse, TechnicalScoreResponse
from services.event_arbitration import EventArbitrator, EventCandidateRef
from services.event_arbitration.models import ArbitratedEvent, ArbitrationResult
from services.player_rating.game_intelligence import (
    GameIntelligenceEngine,
    GameIntelligenceEvidence,
    GameIntelligenceResult,
)


def event_arbitration(result: CompletedResponse) -> ArbitrationResult:
    """Apply the established event arbitration to a completed analysis result."""
    return EventArbitrator().arbitrate(_event_candidates(result))


def game_intelligence_result(
    result: CompletedResponse, arbitration: ArbitrationResult | None = None
) -> GameIntelligenceResult:
    """Evaluate the exact game-intelligence projection used by the public payload."""
    resolved_arbitration = arbitration or EventArbitrator().arbitrate(_event_candidates(result))
    return GameIntelligenceEngine().evaluate(_game_evidence(result, resolved_arbitration))


def _game_evidence(
    result: CompletedResponse, arbitration: ArbitrationResult
) -> GameIntelligenceEvidence:
    selected_player = result.selected_player
    scores = result.scores
    if selected_player is None or scores is None:
        raise ValueError("Available completed responses require selected player and scores.")
    physical = scores.physical
    physical_evidence = physical.evidence if isinstance(physical, PhysicalScoreResponse) else None
    technical = scores.technical
    interaction = result.interaction_analysis
    events = result.technical_event_analysis
    arbitrated = arbitration.events
    passes = [item for item in arbitrated if item.event_type == "pass"]
    shots = [item for item in arbitrated if item.event_type == "shot"]
    return GameIntelligenceEvidence(
        visible_duration_seconds=selected_player.segment_duration_seconds
        or result.video.duration_seconds,
        visibility_ratio=selected_player.visibility_ratio,
        continuity_ratio=(
            result.tracking.longest_continuous_visible_segment
            / max(selected_player.visible_frames, 1)
        ),
        ball_proximity_ratio=selected_player.ball_proximity_ratio,
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
    selected_player = result.selected_player
    if selected_player is None:
        raise ValueError("Available completed responses require a selected player.")
    candidates: list[EventCandidateRef] = []
    for controlled in result.technical_event_analysis.controlled_movement_candidates:
        candidates.append(
            EventCandidateRef(
                controlled.event_id,
                "controlled_movement",
                controlled.start_frame,
                None,
                controlled.end_frame,
                selected_player.track_id,
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
                selected_player.track_id,
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
                selected_player.track_id,
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


def _arbitrated_confidence(events: list[ArbitratedEvent]) -> float | None:
    return sum(item.event_confidence for item in events) / len(events) if events else None
