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
        )
