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
