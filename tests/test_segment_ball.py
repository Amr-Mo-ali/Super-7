"""Deterministic tests for selected-segment ball reconstruction."""

from core.config import Settings
from services.ball_detector import BallDetection
from services.player_detector import BoundingBox
from services.segment_ball import reconstruct


def _ball(frame: int, x: float, confidence: float = 0.9) -> BallDetection:
    box = BoundingBox(x, 0, x + 4, 4)
    return BallDetection(frame, frame / 10, confidence, box, (x + 2, 2))


def test_segment_scope_ignores_global_fragmentation_and_resolves_multiple_candidates() -> None:
    candidates: dict[int, tuple[BallDetection, ...]] = {
        frame: (_ball(frame, float(frame)),) for frame in range(10, 20)
    }
    candidates[14] = (_ball(14, 14), _ball(14, 500, 0.99))
    result = reconstruct(candidates, 10, 19, 10, Settings())
    assert result.quality is not None and result.quality > 0.45
    assert result.points[14].center_point == (16, 2)
    assert result.multiple_candidate_ratio == 0.1


def test_short_gaps_interpolate_but_long_gaps_do_not() -> None:
    result = reconstruct(
        {0: (_ball(0, 0),), 3: (_ball(3, 3),), 7: (_ball(7, 7),)}, 0, 7, 10, Settings()
    )
    assert result.interpolated_frames == 2
    assert 1 in result.points and 2 in result.points
    assert 4 not in result.points


def test_no_segment_observations_is_unavailable() -> None:
    result = reconstruct({}, 10, 20, 10, Settings())
    assert result.quality is None
    assert result.failure_reasons == ("no_accepted_segment_ball_observations",)
