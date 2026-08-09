"""Product-owned deterministic Event Arbitration V0.1 thresholds."""

from typing import Final

MAX_RELEASE_FRAME_DIFFERENCE: Final[int] = 1
MAX_START_FRAME_DIFFERENCE: Final[int] = 2
MAX_END_FRAME_DIFFERENCE: Final[int] = 2
MIN_TEMPORAL_OVERLAP_RATIO: Final[float] = 0.80
RELATIVE_DISTANCE_TOLERANCE: Final[float] = 0.15
DECISIVE_EVENT_EVIDENCE_MARGIN: Final[float] = 0.15
MIN_PASS_TRAJECTORY_QUALITY: Final[float] = 0.40
MIN_SHOT_TRAJECTORY_QUALITY: Final[float] = 0.40
EVENT_ARBITRATION_VERSION: Final[str] = "event_arbitration_v0.1"
