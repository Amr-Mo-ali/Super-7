"""Immutable runtime configuration for the MVP."""

from dataclasses import dataclass, field
from os import environ

from config.debug import DebugSettings
from config.football_profiles import threshold


@dataclass(frozen=True, slots=True)
class Settings:
    """Validated operational limits and version labels."""

    max_upload_bytes: int = 100 * 1024 * 1024
    download_timeout_seconds: float = 30.0
    callback_timeout_seconds: float = 10.0
    video_storage_root: str = "/videos"
    max_duration_seconds: float = 15 * 60
    request_deadline_seconds: float = 15 * 60
    min_width: int = 64
    min_height: int = 64
    min_fps: float = 1.0
    analysis_version: str = "1.0.0"
    model_version: str = "unconfigured"
    visibility_weight: float = 0.65
    ball_proximity_weight: float = 0.35
    selection_margin: float = float(threshold("selection_margin"))
    minimum_visibility_ratio: float = float(threshold("minimum_visibility_ratio"))
    minimum_continuous_track_length: int = int(threshold("minimum_continuous_track_length"))
    minimum_detection_confidence: float = float(threshold("minimum_detection_confidence"))
    target_selection_mode: str = "segment"
    target_segment_max_gap_frames: int = int(threshold("target_segment_max_gap_frames"))
    target_segment_min_visible_frames: int = int(threshold("target_segment_min_visible_frames"))
    target_segment_min_duration_seconds: float = float(
        threshold("target_segment_min_duration_seconds")
    )
    target_segment_min_mean_confidence: float = float(
        threshold("target_segment_min_mean_confidence")
    )
    target_segment_min_quality: float = float(threshold("target_segment_min_quality"))
    target_segment_max_normalized_center_jump: float = float(
        threshold("target_segment_max_normalized_center_jump")
    )
    tracklet_stitching_enabled: bool = False
    segment_ball_max_interpolation_gap_frames: int = int(
        threshold("segment_ball_max_interpolation_gap_frames")
    )
    segment_ball_max_normalized_jump: float = float(threshold("segment_ball_max_normalized_jump"))
    segment_ball_min_endpoint_confidence: float = float(
        threshold("segment_ball_min_endpoint_confidence")
    )
    segment_ball_min_analysis_quality: float = float(threshold("segment_ball_min_analysis_quality"))
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
    ball_minimum_detection_confidence: float = float(threshold("ball_minimum_detection_confidence"))
    ball_smoothing_window: int = 3
    ball_motion_gate: float = 120.0
    ball_proximity_threshold: float = 1.25
    ball_interaction_gap_frames: int = 2
    ball_minimum_visible_frames: int = int(threshold("ball_minimum_visible_frames"))
    ball_minimum_quality: float = float(threshold("ball_minimum_quality"))
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
    interaction_proximity_threshold_ratio: float = float(
        threshold("interaction_proximity_threshold_ratio")
    )
    interaction_min_player_confidence: float = float(threshold("interaction_min_player_confidence"))
    interaction_min_ball_confidence: float = float(threshold("interaction_min_ball_confidence"))
    interaction_max_gap_frames: int = int(threshold("interaction_max_gap_frames"))
    interaction_min_segment_frames: int = int(threshold("interaction_min_segment_frames"))
    interaction_min_segment_duration_seconds: float = float(
        threshold("interaction_min_segment_duration_seconds")
    )
    interaction_duration_scale: float = 1.0
    interaction_distance_weight: float = 0.30
    interaction_coverage_weight: float = 0.20
    interaction_duration_weight: float = 0.15
    interaction_detection_weight: float = 0.15
    interaction_quality_weight: float = 0.20
    interaction_min_ball_analysis_quality: float = float(
        threshold("interaction_min_ball_analysis_quality")
    )
    interaction_min_player_track_quality: float = float(
        threshold("interaction_min_player_track_quality")
    )
    interaction_min_segment_confidence: float = float(
        threshold("interaction_min_segment_confidence")
    )
    interaction_max_returned_segments: int = 100
    technical_event_min_player_track_quality: float = float(
        threshold("technical_event_min_player_track_quality")
    )
    technical_event_min_ball_analysis_quality: float = float(
        threshold("technical_event_min_ball_analysis_quality")
    )
    technical_event_min_interaction_quality: float = float(
        threshold("technical_event_min_interaction_quality")
    )
    technical_event_min_evidence_coverage: float = float(
        threshold("technical_event_min_evidence_coverage")
    )
    controlled_min_duration_seconds: float = float(threshold("controlled_min_duration_seconds"))
    controlled_min_player_displacement_ratio: float = float(
        threshold("controlled_min_player_displacement_ratio")
    )
    controlled_min_ball_proximity_ratio: float = float(
        threshold("controlled_min_ball_proximity_ratio")
    )
    controlled_min_direction_similarity: float = float(
        threshold("controlled_min_direction_similarity")
    )
    controlled_min_evidence_coverage: float = float(threshold("controlled_min_evidence_coverage"))
    controlled_min_confidence: float = float(threshold("controlled_min_confidence"))
    dribble_min_duration_seconds: float = float(threshold("dribble_min_duration_seconds"))
    dribble_min_direction_changes: int = 1
    dribble_min_normalized_displacement: float = 0.50
    dribble_min_proximity_ratio: float = float(threshold("dribble_min_proximity_ratio"))
    dribble_min_confidence: float = float(threshold("dribble_min_confidence"))
    dribble_direct_displacement_scale: float = 0.30
    dribble_path_length_scale: float = 0.60
    dribble_direct_displacement_weight: float = 0.40
    dribble_path_length_weight: float = 0.60
    dribble_directional_min_direction_changes: int = 1
    dribble_progressive_min_movement_component: float = float(
        threshold("dribble_progressive_min_movement_component")
    )
    dribble_progressive_min_duration_seconds: float = 0.60
    dribble_progressive_min_direction_similarity: float = 0.60
    dribble_min_trajectory_quality: float = float(threshold("dribble_min_trajectory_quality"))
    dribble_minimum_direction_change_angle_degrees: float = 35.0
    dribble_minimum_turn_frame_separation: int = 4
    dribble_max_direction_changes_per_second: float = float(
        threshold("dribble_max_direction_changes_per_second")
    )
    dribble_progressive_min_normalized_displacement: float = float(
        threshold("dribble_progressive_min_normalized_displacement")
    )
    dribble_progressive_min_path_straightness: float = float(
        threshold("dribble_progressive_min_path_straightness")
    )
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
    debug_output_dir: str = "debug"
    debug: DebugSettings = field(default_factory=DebugSettings)
    pass_possession_proximity_ratio: float = 1.2
    pass_min_possession_frames: int = 3
    pass_max_gap_frames: int = 2
    pass_release_window_frames: int = 6
    pass_min_release_speed_pixels: float = 20.0
    pass_max_trajectory_frames: int = 90
    pass_min_trajectory_frames: int = 3
    pass_min_trajectory_length_pixels: float = 30.0
    pass_trajectory_quality_length_pixels: float = 150.0
    pass_receiver_proximity_ratio: float = 1.5

    def __post_init__(self) -> None:
        if self.download_timeout_seconds <= 0:
            raise ValueError("download_timeout_seconds must be positive.")
        if self.callback_timeout_seconds <= 0:
            raise ValueError("callback_timeout_seconds must be positive.")
        if self.request_deadline_seconds <= 0:
            raise ValueError("request_deadline_seconds must be positive.")

    @classmethod
    def from_environment(cls) -> "Settings":
        """Load optional operational limits from environment variables."""
        return cls(
            max_upload_bytes=int(environ.get("MAX_UPLOAD_BYTES", 100 * 1024 * 1024)),
            download_timeout_seconds=float(environ.get("DOWNLOAD_TIMEOUT_SECONDS", 30.0)),
            callback_timeout_seconds=float(environ.get("CALLBACK_TIMEOUT_SECONDS", 10.0)),
            video_storage_root=environ.get("VIDEO_STORAGE_ROOT", "/videos"),
            max_duration_seconds=float(environ.get("MAX_DURATION_SECONDS", 15 * 60)),
            request_deadline_seconds=float(environ.get("REQUEST_DEADLINE_SECONDS", 15 * 60)),
            model_path=environ.get("MODEL_PATH", "yolo11n.pt"),
            model_device=environ.get("MODEL_DEVICE", "cpu"),
            model_confidence=float(environ.get("MODEL_CONFIDENCE", 0.25)),
            model_iou=float(environ.get("MODEL_IOU", 0.45)),
            model_image_size=int(environ.get("MODEL_IMAGE_SIZE", 640)),
            ball_model_path=environ.get("BALL_MODEL_PATH", environ.get("MODEL_PATH", "yolo11n.pt")),
            ball_confidence=float(environ.get("BALL_CONFIDENCE", 0.15)),
            ball_iou=float(environ.get("BALL_IOU", 0.45)),
            ball_image_size=int(environ.get("BALL_IMAGE_SIZE", 640)),
            target_selection_mode=environ.get("TARGET_SELECTION_MODE", "segment"),
            target_segment_max_gap_frames=int(environ.get("TARGET_SEGMENT_MAX_GAP_FRAMES", 3)),
            target_segment_min_visible_frames=int(
                environ.get("TARGET_SEGMENT_MIN_VISIBLE_FRAMES", 30)
            ),
            target_segment_min_duration_seconds=float(
                environ.get("TARGET_SEGMENT_MIN_DURATION_SECONDS", 1.0)
            ),
            target_segment_min_mean_confidence=float(
                environ.get("TARGET_SEGMENT_MIN_MEAN_CONFIDENCE", 0.30)
            ),
            target_segment_min_quality=float(environ.get("TARGET_SEGMENT_MIN_QUALITY", 0.45)),
            tracklet_stitching_enabled=environ.get("TRACKLET_STITCHING_ENABLED", "false").lower()
            == "true",
            segment_ball_max_interpolation_gap_frames=int(
                environ.get("SEGMENT_BALL_MAX_INTERPOLATION_GAP_FRAMES", 2)
            ),
            segment_ball_max_normalized_jump=float(
                environ.get("SEGMENT_BALL_MAX_NORMALIZED_JUMP", 3.0)
            ),
            segment_ball_min_endpoint_confidence=float(
                environ.get("SEGMENT_BALL_MIN_ENDPOINT_CONFIDENCE", 0.25)
            ),
            segment_ball_min_analysis_quality=float(
                environ.get("SEGMENT_BALL_MIN_ANALYSIS_QUALITY", 0.45)
            ),
            debug=DebugSettings.from_environment(),
        )
