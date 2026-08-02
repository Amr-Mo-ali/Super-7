"""Immutable runtime configuration for the MVP."""

from dataclasses import dataclass
from os import environ


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated operational limits and version labels."""

    max_upload_bytes: int = 100 * 1024 * 1024
    max_duration_seconds: float = 15 * 60
    min_width: int = 64
    min_height: int = 64
    min_fps: float = 1.0
    analysis_version: str = "1.0.0"
    model_version: str = "unconfigured"
    visibility_weight: float = 0.65
    ball_proximity_weight: float = 0.35
    selection_margin: float = 0.08
    minimum_visibility_ratio: float = 0.20
    minimum_continuous_track_length: int = 5
    minimum_detection_confidence: float = 0.50
    model_path: str = "yolo11n.pt"
    model_device: str = "cpu"
    model_confidence: float = 0.25
    model_iou: float = 0.45
    model_image_size: int = 640
    tracker_high_threshold: float = 0.25
    tracker_low_threshold: float = 0.10
    tracker_match_threshold: float = 0.80
    tracker_buffer: int = 30
    minimum_track_frames: int = 5
    ball_model_path: str = "yolo11n.pt"
    ball_confidence: float = 0.15
    ball_iou: float = 0.45
    ball_image_size: int = 640
    ball_max_center_displacement: float = 120.0
    ball_max_missing_frames: int = 8
    ball_minimum_detection_confidence: float = 0.15
    ball_smoothing_window: int = 3
    ball_motion_gate: float = 120.0
    ball_proximity_threshold: float = 1.25
    ball_interaction_gap_frames: int = 2
    ball_minimum_visible_frames: int = 3
    ball_minimum_quality: float = 0.30
    movement_smoothing_window: int = 5
    movement_max_normalized_jump: float = 3.0
    movement_direction_change_degrees: float = 30.0
    movement_minimum_vector_pixels: float = 2.0
    movement_stationary_speed: float = 5.0
    movement_distance_normalizer: float = 1000.0
    movement_speed_normalizer: float = 100.0
    movement_acceleration_normalizer: float = 300.0
    movement_min_stationary_duration_seconds: float = 0.3
    movement_frame_diagonal_pixels: float = 1175.0
    movement_distance_rate_scale: float = 0.08
    movement_speed_rate_scale: float = 0.08
    movement_distance_weight: float = 0.40
    movement_speed_weight: float = 0.35
    movement_activity_weight: float = 0.25
    movement_minimum_quality: float = 0.20
    movement_raw_image_space_quality_cap: float = 0.80
    interaction_proximity_threshold_ratio: float = 1.20
    interaction_min_player_confidence: float = 0.25
    interaction_min_ball_confidence: float = 0.25
    interaction_max_gap_frames: int = 2
    interaction_min_segment_frames: int = 5
    interaction_min_segment_duration_seconds: float = 0.15
    interaction_duration_scale: float = 1.0
    interaction_distance_weight: float = 0.30
    interaction_coverage_weight: float = 0.20
    interaction_duration_weight: float = 0.15
    interaction_detection_weight: float = 0.15
    interaction_quality_weight: float = 0.20
    interaction_min_ball_analysis_quality: float = 0.50
    interaction_min_player_track_quality: float = 0.50
    interaction_min_segment_confidence: float = 0.45
    interaction_max_returned_segments: int = 100
    technical_event_min_player_track_quality: float = 0.50
    technical_event_min_ball_analysis_quality: float = 0.50
    technical_event_min_interaction_quality: float = 0.50
    technical_event_min_evidence_coverage: float = 0.60
    controlled_min_duration_seconds: float = 0.40
    controlled_min_player_displacement_ratio: float = 0.30
    controlled_min_ball_proximity_ratio: float = 0.70
    controlled_min_direction_similarity: float = 0.50
    controlled_min_evidence_coverage: float = 0.70
    controlled_min_confidence: float = 0.50
    dribble_min_duration_seconds: float = 0.60
    dribble_min_direction_changes: int = 1
    dribble_min_normalized_displacement: float = 0.50
    dribble_min_proximity_ratio: float = 0.75
    dribble_min_confidence: float = 0.55
    ball_loss_min_pre_interaction_seconds: float = 0.30
    ball_loss_min_separation_ratio: float = 1.50
    ball_loss_min_ball_away_speed_normalized: float = 0.20
    ball_loss_recovery_window_seconds: float = 0.50
    ball_loss_min_post_evidence_frames: int = 3
    ball_loss_min_confidence: float = 0.50
    technical_event_max_returned_events: int = 100
    physical_score_activity_weight: float = 0.35
    physical_score_active_time_weight: float = 0.25
    physical_score_visibility_weight: float = 0.15
    physical_score_continuity_weight: float = 0.15
    physical_score_direction_weight: float = 0.10
    physical_score_direction_rate_scale: float = 0.5
    physical_score_min_movement_quality: float = 0.55
    physical_score_min_visibility_ratio: float = 0.20
    physical_score_min_visible_seconds: float = 3.0
    physical_score_min_movement_observations: int = 30
    physical_score_min_accepted_interval_ratio: float = 0.60
    physical_score_raw_image_confidence_cap: float = 0.75

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load optional operational limits from environment variables."""
        return cls(
            max_upload_bytes=int(environ.get("MAX_UPLOAD_BYTES", 100 * 1024 * 1024)),
            max_duration_seconds=float(environ.get("MAX_DURATION_SECONDS", 15 * 60)),
            model_path=environ.get("MODEL_PATH", "yolo11n.pt"),
            model_device=environ.get("MODEL_DEVICE", "cpu"),
            model_confidence=float(environ.get("MODEL_CONFIDENCE", 0.25)),
            model_iou=float(environ.get("MODEL_IOU", 0.45)),
            model_image_size=int(environ.get("MODEL_IMAGE_SIZE", 640)),
            ball_model_path=environ.get("BALL_MODEL_PATH", environ.get("MODEL_PATH", "yolo11n.pt")),
            ball_confidence=float(environ.get("BALL_CONFIDENCE", 0.15)),
            ball_iou=float(environ.get("BALL_IOU", 0.45)),
            ball_image_size=int(environ.get("BALL_IMAGE_SIZE", 640)),
        )
