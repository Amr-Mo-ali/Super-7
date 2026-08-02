"""Internal movement value objects."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MovementPoint:
    frame_index: int
    timestamp_seconds: float
    position: tuple[float, float]
    bbox_height: float


@dataclass(frozen=True, slots=True)
class MovementMetrics:
    covered_distance: float
    average_speed: float
    maximum_speed: float
    average_acceleration: float
    maximum_acceleration: float
    direction_changes: int
    mean_direction_change: float
    stationary_period_count: int
    stationary_time_seconds: float
    longest_stationary_duration: float
    movement_intensity: float
    distance_component: float
    speed_component: float
    activity_component: float
    raw_movement_intensity: float
    stationary_frames: int
    raw_stationary_segments: int
    rejected_short_stationary_segments: int


@dataclass(frozen=True, slots=True)
class MovementResult:
    metrics: MovementMetrics
    trajectory: tuple[MovementPoint, ...]
    movement_segments: int
    rejected_position_jumps: int
    smoothed_positions: int
