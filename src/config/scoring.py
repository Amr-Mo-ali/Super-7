"""Product-owned provisional scoring constants."""

from typing import Final

GAME_INTELLIGENCE_WEIGHTS: Final[dict[str, float]] = {
    "ball_involvement": 0.30,
    "decision_consistency": 0.20,
    "spatial_activity_proxy": 0.20,
    "movement_efficiency_proxy": 0.15,
    "technical_involvement": 0.15,
}
MIN_AVAILABLE_GAME_INTELLIGENCE_COMPONENTS: Final[int] = 3
MIN_GAME_INTELLIGENCE_VISIBLE_DURATION_SECONDS: Final[float] = 4.0
PREFERRED_GAME_INTELLIGENCE_DURATION_SECONDS: Final[float] = 20.0
MAX_GAME_INTELLIGENCE_CONFIDENCE: Final[float] = 0.65
