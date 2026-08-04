"""Threshold profiles.  Values in BALANCED_PROFILE preserve the V0.1 behaviour."""

from copy import deepcopy
from typing import Final

BALANCED_PROFILE: Final[dict[str, dict[str, float | int]]] = {
    "player_selection": {
        "selection_margin": 0.08,
        "minimum_visibility_ratio": 0.20,
        "minimum_continuous_track_length": 5,
        "minimum_detection_confidence": 0.50,
        "target_segment_max_gap_frames": 3,
        "target_segment_min_visible_frames": 30,
        "target_segment_min_duration_seconds": 1.0,
        "target_segment_min_mean_confidence": 0.30,
        "target_segment_min_quality": 0.45,
        "target_segment_max_normalized_center_jump": 3.0,
    },
    "ball": {
        "ball_minimum_detection_confidence": 0.15,
        "ball_minimum_visible_frames": 3,
        "ball_minimum_quality": 0.30,
        "segment_ball_max_interpolation_gap_frames": 2,
        "segment_ball_max_normalized_jump": 3.0,
        "segment_ball_min_endpoint_confidence": 0.25,
        "segment_ball_min_analysis_quality": 0.45,
    },
    "interaction": {
        "interaction_proximity_threshold_ratio": 1.20,
        "interaction_min_player_confidence": 0.25,
        "interaction_min_ball_confidence": 0.25,
        "interaction_max_gap_frames": 2,
        "interaction_min_segment_frames": 5,
        "interaction_min_segment_duration_seconds": 0.15,
        "interaction_min_ball_analysis_quality": 0.50,
        "interaction_min_player_track_quality": 0.50,
        "interaction_min_segment_confidence": 0.45,
    },
    "controlled_movement": {
        "technical_event_min_player_track_quality": 0.50,
        "technical_event_min_ball_analysis_quality": 0.50,
        "technical_event_min_interaction_quality": 0.50,
        "technical_event_min_evidence_coverage": 0.60,
        "controlled_min_duration_seconds": 0.25,
        "controlled_min_player_displacement_ratio": 0.15,
        "controlled_min_ball_proximity_ratio": 0.70,
        "controlled_min_direction_similarity": 0.35,
        "controlled_min_evidence_coverage": 0.70,
        "controlled_min_confidence": 0.50,
    },
    "dribble": {
        "dribble_min_duration_seconds": 0.60,
        "dribble_min_proximity_ratio": 0.75,
        "dribble_min_confidence": 0.55,
        "dribble_progressive_min_movement_component": 0.35,
        "dribble_min_trajectory_quality": 0.60,
        "dribble_max_direction_changes_per_second": 4.0,
        "dribble_progressive_min_normalized_displacement": 0.30,
        "dribble_progressive_min_path_straightness": 0.55,
    },
    "technical_scoring": {
        "technical_event_max_returned_events": 100,
        "interaction_max_returned_segments": 100,
    },
}

# Profiles deliberately retain the V0.1 values until a separately reviewed calibration
# changes them.  Their identity is nevertheless recorded with every analysis.
CONSERVATIVE_PROFILE: Final[dict[str, dict[str, float | int]]] = deepcopy(BALANCED_PROFILE)
AGGRESSIVE_PROFILE: Final[dict[str, dict[str, float | int]]] = deepcopy(BALANCED_PROFILE)
ACTIVE_PROFILE: Final[dict[str, dict[str, float | int]]] = BALANCED_PROFILE
ACTIVE_PROFILE_NAME: Final[str] = "balanced"


def threshold(name: str) -> float | int:
    """Get a configured threshold by its stable Settings field name."""
    for group in ACTIVE_PROFILE.values():
        if name in group:
            return group[name]
    raise KeyError(f"Threshold {name!r} is not in the active football profile")
