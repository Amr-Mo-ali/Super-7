"""Response contracts for automatic target selection."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class AnalyzeRequest(BaseModel):
    """Backend-owned identifiers and delivery details for one analysis request."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    video_id: str = Field(alias="videoId")
    player_id: str = Field(alias="playerId")
    video_url: str = Field(alias="videoUrl")
    callback_url: HttpUrl = Field(alias="callbackUrl")


class AnalyzeAcceptedResponse(BaseModel):
    """Synchronous acknowledgement of a validated backend request."""

    request_id: str
    video_id: str
    player_id: str
    status: Literal["accepted"] = "accepted"


class AnalyzeQueuedResponse(BaseModel):
    """Immediate acknowledgement for an accepted background analysis job."""

    model_config = ConfigDict(populate_by_name=True)

    analysis_id: str = Field(serialization_alias="analysisId")
    video_id: str = Field(serialization_alias="videoId")
    player_id: str = Field(serialization_alias="playerId")
    status: Literal["queued"] = "queued"


class VideoResponse(BaseModel):
    duration_seconds: float
    fps: float
    width: int
    height: int


class StageGate(BaseModel):
    quality: float = Field(ge=0, le=1)
    status: Literal["accepted", "rejected", "skipped", "failed"]
    failure_reasons: list[str] = Field(default_factory=list)


class PipelineTiming(BaseModel):
    player_detection_time_ms: int = 0
    tracking_time_ms: int = 0
    segment_selection_time_ms: int = 0
    ball_processing_time_ms: int = 0
    interaction_processing_time_ms: int = 0
    controlled_movement_time_ms: int = 0
    dribble_processing_time_ms: int = 0
    technical_scoring_time_ms: int = 0
    physical_scoring_time_ms: int = 0
    total_processing_time_ms: int = 0


class UnsupportedMetric(BaseModel):
    value: None = None
    reason: str


class PhysicalScoreEvidenceResponse(BaseModel):
    movement_intensity: float
    active_time_ratio: float
    visibility_ratio: float
    continuity_ratio: float
    direction_component: float
    movement_analysis_quality: float
    movement_duration_seconds: float
    movement_observations: int
    accepted_interval_ratio: float


class PhysicalScoreResponse(BaseModel):
    value: float | None = Field(default=None, ge=0, le=100)
    level: int | None = Field(default=None, ge=1, le=7)
    level_label: str | None = None
    level_midpoint: float | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: str
    version: str
    reason: str | None = None
    evidence: PhysicalScoreEvidenceResponse | None = None
    limitations: list[str] = Field(default_factory=list)
    explanation: str


class TechnicalScoreResponse(BaseModel):
    value: float = Field(ge=0, le=100)
    confidence: float = Field(ge=0, le=1)
    status: str
    version: str
    evidence: dict[str, float]


class FeatureMetric(BaseModel):
    value: float | None = None
    reason: str | None = None


class SelectedPlayer(BaseModel):
    track_id: int
    selection_method: str
    selection_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    visible_frames: int
    visibility_ratio: float
    ball_proximity_frames: int
    ball_proximity_ratio: float
    visibility_contribution: float
    ball_proximity_contribution: float
    segment_id: int | None = None
    segment_start_frame: int | None = None
    segment_end_frame: int | None = None
    segment_duration_seconds: float | None = None


class TrackingResponse(BaseModel):
    frames_processed: int
    lost_track_count: int
    longest_continuous_visible_segment: int


class FeaturesResponse(BaseModel):
    ball_proximity_time_seconds: FeatureMetric
    movement_intensity: FeatureMetric
    direction_changes: FeatureMetric
    average_speed: FeatureMetric = Field(default_factory=FeatureMetric)
    max_speed: FeatureMetric = Field(default_factory=FeatureMetric)
    covered_distance: FeatureMetric = Field(default_factory=FeatureMetric)
    stationary_periods: FeatureMetric = Field(default_factory=FeatureMetric)
    covered_distance_pixels: FeatureMetric = Field(default_factory=FeatureMetric)
    average_speed_pixels_per_second: FeatureMetric = Field(default_factory=FeatureMetric)
    max_speed_pixels_per_second: FeatureMetric = Field(default_factory=FeatureMetric)
    stationary_time_seconds: FeatureMetric = Field(default_factory=FeatureMetric)
    average_acceleration_pixels_per_second_squared: FeatureMetric = Field(
        default_factory=FeatureMetric
    )
    max_acceleration_pixels_per_second_squared: FeatureMetric = Field(default_factory=FeatureMetric)


class InteractionSegmentResponse(BaseModel):
    segment_id: int
    start_frame: int
    end_frame: int
    start_time_seconds: float
    end_time_seconds: float
    duration_seconds: float
    observed_frame_count: int
    bridged_gap_frames: int
    candidate_frame_count: int
    evidence_coverage_ratio: float = Field(ge=0, le=1)
    mean_distance_pixels: float
    minimum_distance_pixels: float
    mean_normalized_distance: float
    minimum_normalized_distance: float
    mean_player_confidence: float = Field(ge=0, le=1)
    mean_ball_confidence: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    status: Literal["possible_ball_interaction"]


class InteractionAnalysisResponse(BaseModel):
    possible_ball_interaction_count: FeatureMetric = Field(default_factory=FeatureMetric)
    possible_ball_interaction_time_seconds: FeatureMetric = Field(default_factory=FeatureMetric)
    longest_possible_ball_interaction_seconds: FeatureMetric = Field(default_factory=FeatureMetric)
    mean_possible_ball_interaction_confidence: FeatureMetric = Field(default_factory=FeatureMetric)
    interaction_candidate_frames: FeatureMetric = Field(default_factory=FeatureMetric)
    interaction_observed_frames: FeatureMetric = Field(default_factory=FeatureMetric)
    interaction_evidence_coverage_ratio: FeatureMetric = Field(default_factory=FeatureMetric)
    segments: list[InteractionSegmentResponse] = Field(default_factory=list)
    confidence_version: str = "interaction_confidence_v0.1"


class ControlledMovementCandidateResponse(BaseModel):
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
    confidence: float = Field(ge=0, le=1)
    status: Literal["controlled_movement_candidate"]


class DribbleCandidateResponse(BaseModel):
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
    confidence: float = Field(ge=0, le=1)
    confidence_version: str
    status: Literal["dribble_candidate"]


class BallLossCandidateResponse(BaseModel):
    event_id: str
    source_interaction_segment_id: int
    event_frame: int
    event_time_seconds: float
    pre_interaction_duration_seconds: float
    maximum_separation_ratio: float
    post_evidence_frames: int
    recovered_within_window: bool
    confidence: float = Field(ge=0, le=1)
    status: Literal["ball_loss_candidate"]


class TechnicalEventAnalysisResponse(BaseModel):
    controlled_movement_candidate_count: FeatureMetric = Field(default_factory=FeatureMetric)
    controlled_movement_candidate_time_seconds: FeatureMetric = Field(default_factory=FeatureMetric)
    mean_controlled_movement_confidence: FeatureMetric = Field(default_factory=FeatureMetric)
    dribble_candidate_count: FeatureMetric = Field(default_factory=FeatureMetric)
    dribble_candidate_time_seconds: FeatureMetric = Field(default_factory=FeatureMetric)
    mean_dribble_candidate_confidence: FeatureMetric = Field(default_factory=FeatureMetric)
    ball_loss_candidate_count: FeatureMetric = Field(default_factory=FeatureMetric)
    mean_ball_loss_candidate_confidence: FeatureMetric = Field(default_factory=FeatureMetric)
    controlled_movement_candidates: list[ControlledMovementCandidateResponse] = Field(
        default_factory=list
    )
    dribble_candidates: list[DribbleCandidateResponse] = Field(default_factory=list)
    ball_loss_candidates: list[BallLossCandidateResponse] = Field(default_factory=list)
    versions: dict[str, str] = Field(
        default_factory=lambda: {
            "controlled_movement": "controlled_movement_confidence_v0.1",
            "dribble": "dribble_candidate_confidence_v0.2",
            "ball_loss": "ball_loss_candidate_confidence_v0.1",
        }
    )


class PassCandidateResponse(BaseModel):
    pass_id: str
    possessor_track_id: int
    receiver_track_id: int
    start_frame: int
    release_frame: int
    end_frame: int
    duration_seconds: float
    distance: float
    confidence: float = Field(ge=0, le=1)
    release_speed: float
    trajectory_points: list[tuple[float, float]]
    trajectory_duration: float
    trajectory_length: float
    trajectory_direction: tuple[float, float]
    trajectory_quality: float = Field(ge=0, le=1)
    status: str


class PassDetectionResponse(BaseModel):
    pass_candidates: list[PassCandidateResponse] = Field(default_factory=list)
    raw_pass_candidates: int = 0
    accepted_pass_candidates: int = 0
    rejected_pass_candidates: int = 0
    rejection_breakdown: dict[str, int] = Field(default_factory=dict)
    pass_detection_version: str = "pass_detection_v0.1"
    processing_time_ms: int = 0


class ShotCandidateResponse(BaseModel):
    shot_id: str
    possessor_track_id: int
    start_frame: int
    preparation_start_frame: int
    preparation_end_frame: int
    release_frame: int
    end_frame: int
    duration_seconds: float
    distance: float
    trajectory_points: list[tuple[float, float]]
    trajectory_duration: float
    mean_speed: float
    maximum_speed: float
    release_speed: float
    release_direction: tuple[float, float]
    release_acceleration: float
    preparation_confidence: float = Field(ge=0, le=1)
    release_confidence: float = Field(ge=0, le=1)
    trajectory_quality: float = Field(ge=0, le=1)
    follow_through_confidence: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    status: str


class ShotDetectionResponse(BaseModel):
    shot_candidates: list[ShotCandidateResponse] = Field(default_factory=list)
    raw_shot_candidates: int = 0
    accepted_shot_candidates: int = 0
    rejected_shot_candidates: int = 0
    rejection_breakdown: dict[str, int] = Field(default_factory=dict)
    shot_detection_version: str = "shot_detection_v0.1"
    processing_time_ms: int = 0


class ScoresResponse(BaseModel):
    technical: TechnicalScoreResponse | UnsupportedMetric
    physical: PhysicalScoreResponse | UnsupportedMetric
    game_intelligence: UnsupportedMetric
    mental_resilience: UnsupportedMetric
    professionalism: UnsupportedMetric
    growth_potential: UnsupportedMetric
    market_readiness: UnsupportedMetric


class CompletedResponse(BaseModel):
    analysis_id: str
    status: Literal["completed"]
    video: VideoResponse
    selected_player: SelectedPlayer
    tracking: TrackingResponse
    features: FeaturesResponse
    interaction_analysis: InteractionAnalysisResponse = Field(
        default_factory=InteractionAnalysisResponse
    )
    technical_event_analysis: TechnicalEventAnalysisResponse = Field(
        default_factory=TechnicalEventAnalysisResponse
    )
    pass_detection: PassDetectionResponse = Field(default_factory=PassDetectionResponse)
    shot_detection: ShotDetectionResponse = Field(default_factory=ShotDetectionResponse)
    scores: ScoresResponse
    diagnostics: "Diagnostics"
    warnings: list[str]
    analysis_version: str
    model_version: str
    processing_time_ms: int
    pipeline_state: Literal["COMPLETE"] = "COMPLETE"
    algorithm_versions: dict[str, str] = Field(default_factory=dict)
    timing: PipelineTiming = Field(default_factory=PipelineTiming)
    quality_gates: dict[str, StageGate] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    analysis_metadata: dict[str, str | None] = Field(default_factory=dict)
    debug_artifacts: dict[str, str] = Field(default_factory=dict)


class AmbiguousResponse(BaseModel):
    analysis_id: str
    status: Literal["ambiguous_target"]
    selected_player: None = None
    candidate_count: int
    warnings: list[str]
    metadata: dict[str, Any] = Field(default_factory=dict)


class Diagnostics(BaseModel):
    frames_processed: int
    frames_with_player_detections: int
    total_person_detections: int
    tracks_created: int
    valid_candidate_tracks: int
    ball_detections: int
    ball_visible_frames: int = 0
    ball_track_segments: int = 0
    ball_detection_confidence_mean: float | None = None
    raw_ball_detections: int = 0
    filtered_ball_detections: int = 0
    accepted_ball_track_observations: int = 0
    frames_with_multiple_ball_candidates: int = 0
    rejected_ball_candidates: int = 0
    unique_track_ids: int = 0
    selected_track_visible_frames: int | None = None
    ball_analysis_quality: float | None = None
    movement_frames: int = 0
    movement_segments: int = 0
    rejected_position_jumps: int = 0
    smoothed_positions: int = 0
    average_speed: float | None = None
    maximum_speed: float | None = None
    movement_observations: int = 0
    movement_duration_seconds: float | None = None
    stationary_frames: int = 0
    movement_scoring_version: str | None = None
    raw_stationary_segments: int = 0
    accepted_stationary_segments: int = 0
    rejected_short_stationary_segments: int = 0
    distance_component: float | None = None
    speed_component: float | None = None
    activity_component: float | None = None
    raw_movement_intensity: float | None = None
    clamped_movement_intensity: float | None = None
    movement_intensity_saturated: bool = False
    movement_analysis_quality: float | None = None
    camera_motion_enabled: bool = False
    camera_motion_evaluated_intervals: int = 0
    camera_motion_accepted_intervals: int = 0
    camera_motion_rejected_intervals: int = 0
    camera_motion_coverage_ratio: float = 0.0
    camera_motion_mean_confidence: float | None = None
    camera_motion: dict[str, object] | None = None
    trajectory_validation: list[dict[str, object]] = Field(default_factory=list)
    movement_metrics_source: str = "raw_image_space"
    interaction_aligned_frames: int = 0
    interaction_candidate_frames: int = 0
    interaction_non_candidate_frames: int = 0
    interaction_missing_evidence_frames: int = 0
    raw_interaction_segments: int = 0
    accepted_interaction_segments: int = 0
    rejected_short_interaction_segments: int = 0
    rejected_low_confidence_interaction_segments: int = 0
    bridged_interaction_gaps: int = 0
    maximum_bridged_gap_frames: int = 0
    interaction_evidence_coverage_ratio: float = 0.0
    interaction_confidence_version: str | None = None
    interaction_analysis_quality: float | None = None
    interaction_processing_time_ms: int = 0
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
    technical_event_analysis_quality: float | None = None
    technical_event_processing_time_ms: int = 0
    controlled_movement_rejection_breakdown: dict[str, int] | None = None
    controlled_movement_thresholds: dict[str, float] | None = None
    controlled_movement_segment_statistics: list[dict[str, float | int | bool | str | None]] = (
        Field(default_factory=list)
    )
    displacement_summary: dict[str, float] | None = None
    displacement_histogram: dict[str, int] | None = None
    dribble_candidate_statistics: list[dict[str, float | int | bool | str | None]] = Field(
        default_factory=list
    )
    dribble_rejection_breakdown: dict[str, int] | None = None
    dribble_thresholds: dict[str, float] | None = None
    physical_score_version: str | None = None
    physical_confidence_version: str | None = None
    physical_score_raw: float | None = None
    physical_score_final: float | None = None
    physical_score_confidence: float | None = None
    physical_score_level: int | None = None
    physical_score_quality_gate_passed: bool = False
    physical_score_confidence_capped: bool = False
    physical_score_components: dict[str, float] | None = None
    physical_score_processing_time_ms: int = 0
    ball_analysis_scope: str = "full_video"
    ball_quality_scope: str = "full_video"
    global_ball_analysis_quality: float | None = None
    selected_segment_ball_analysis_quality: float | None = None
    ball_quality_failure_reasons: list[str] = Field(default_factory=list)
    segment_ball_total_frames: int = 0
    segment_ball_detected_frames: int = 0
    segment_ball_interpolated_frames: int = 0
    segment_ball_reconstructed_frames: int = 0
    segment_ball_visibility_ratio: float = 0.0
    segment_ball_track_segments_before_reconstruction: int = 0
    segment_ball_track_segments_after_reconstruction: int = 0
    segment_ball_longest_run_frames: int = 0
    segment_ball_longest_gap_frames: int = 0
    segment_ball_mean_confidence: float | None = None
    segment_ball_multiple_candidate_ratio: float = 0.0
    segment_ball_quality_components: dict[str, float] | None = None
    segment_ball_reconstruction_version: str | None = None
    segment_ball_quality_version: str | None = None
    segment_ball_processing_time_ms: int = 0
    rejected_low_global_quality_interaction_segments: int = 0
    rejected_invalid_interaction_segments: int = 0
    rejected_tracks: list[dict[str, object]] = Field(default_factory=list)
    rejected_track_reason_breakdown: dict[str, int] = Field(default_factory=dict)
    technical_evidence_score: float = 0.0
    technical_event_count: int = 0
    technical_controlled_component: float | None = None
    technical_dribble_component: float | None = None
    technical_ball_loss_penalty: float = 0.0
    technical_score_quality: float = 0.0


class NonCompletedResponse(BaseModel):
    analysis_id: str
    status: Literal[
        "invalid_video",
        "no_players_detected",
        "no_valid_tracks",
        "failed",
        "player_detection_completed_tracking_not_available",
    ]
    selected_player: None = None
    candidate_count: int = 0
    warnings: list[str]
    diagnostics: Diagnostics
    pipeline_state: str = "VIDEO"
    quality_gates: dict[str, StageGate] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


AnalyzeResponse = CompletedResponse | AmbiguousResponse | NonCompletedResponse
