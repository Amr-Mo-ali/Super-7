"""Official nearest-midpoint level mapping with deterministic lower tie-breaks."""

LEVELS = (
    (1, "beginner", 25.0),
    (2, "acceptable", 55.0),
    (3, "average", 65.0),
    (4, "good", 75.0),
    (5, "very_good", 85.0),
    (6, "excellent", 92.0),
    (7, "exceptional", 97.5),
)


class ScoreLevelMapper:
    def map(self, value: float) -> tuple[int, str, float]:
        return min(LEVELS, key=lambda item: (abs(value - item[2]), item[0]))
