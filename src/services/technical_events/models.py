"""Framework-neutral technical-event result models."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class TechnicalEvidenceDiagnostics:
    """Inputs captured at the technical-event evidence gate."""

    player_track_quality: float
    ball_analysis_quality: float
    interaction_analysis_quality: float
    interaction_evidence_coverage_ratio: float
    thresholds: dict[str, float]
    failed_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ControlledMovementCandidate:
    event_id: str
    source_interaction_segment_id: int
    start_frame: int
    end_frame: int
    start_time_seconds: float
    end_time_seconds: float
    duration_seconds: float
    player_displacement_pixels: float
    normalized_player_displacement: float
    ball_displacement_pixels: float
    proximity_frame_ratio: float
    direction_similarity: float | None
    confidence: float
    status: Literal["controlled_movement_candidate"] = "controlled_movement_candidate"


@dataclass(frozen=True, slots=True)
class DribbleCandidate:
    event_id: str
    source_controlled_movement_id: str
    start_frame: int
    end_frame: int
    duration_seconds: float
    direction_changes: int
    normalized_player_displacement: float
    normalized_player_path_length: float
    movement_evidence_component: float
    candidate_subtype: Literal["directional_dribble_candidate", "progressive_carry_candidate"]
    proximity_persistence: float
    path_straightness: float
    confidence: float
    confidence_version: str
    status: Literal["dribble_candidate"] = "dribble_candidate"


@dataclass(frozen=True, slots=True)
class BallLossCandidate:
    event_id: str
    source_interaction_segment_id: int
    event_frame: int
    event_time_seconds: float
    pre_interaction_duration_seconds: float
    maximum_separation_ratio: float
    post_evidence_frames: int
    recovered_within_window: bool
    confidence: float
    status: Literal["ball_loss_candidate"] = "ball_loss_candidate"


@dataclass(frozen=True, slots=True)
class TechnicalEventDiagnostics:
    controlled_movement_raw_candidates: int = 0
    controlled_movement_accepted_candidates: int = 0
    controlled_movement_rejected_short: int = 0
    controlled_movement_rejected_low_confidence: int = 0
    dribble_raw_candidates: int = 0
    dribble_accepted_candidates: int = 0
    dribble_rejected_low_movement: int = 0
    dribble_rejected_low_confidence: int = 0
    ball_loss_raw_candidates: int = 0
    ball_loss_accepted_candidates: int = 0
    ball_loss_rejected_missing_post_evidence: int = 0
    ball_loss_rejected_recovery: int = 0
    technical_event_analysis_quality: float = 0.0
    processing_time_ms: int = 0
    controlled_movement_rejection_breakdown: dict[str, int] | None = None
    controlled_movement_thresholds: dict[str, float] | None = None
    controlled_movement_segment_statistics: tuple[
        dict[str, float | int | bool | str | None], ...
    ] = ()
    displacement_summary: dict[str, float] | None = None
    displacement_histogram: dict[str, int] | None = None
    dribble_candidate_statistics: tuple[dict[str, float | int | bool | str | None], ...] = ()
    dribble_rejection_breakdown: dict[str, int] | None = None
    dribble_thresholds: dict[str, float] | None = None
    evidence_gate: TechnicalEvidenceDiagnostics | None = None


@dataclass(frozen=True, slots=True)
class TechnicalEventAnalysisResult:
    controlled_movement_candidates: tuple[ControlledMovementCandidate, ...]
    dribble_candidates: tuple[DribbleCandidate, ...]
    ball_loss_candidates: tuple[BallLossCandidate, ...]
    diagnostics: TechnicalEventDiagnostics
    warnings: tuple[str, ...]
    reason: str | None = None
