"""Invariant tests for raw and unavailable compensated coordinates."""

from services.trajectory_compensation import NoOpTrajectoryCompensator


def test_noop_compensation_preserves_raw_coordinates_and_marks_unavailable() -> None:
    observation = NoOpTrajectoryCompensator().compensate(17, (12.5, 9.25))

    assert observation.frame_index == 17
    assert (observation.raw_center_x, observation.raw_center_y) == (12.5, 9.25)
    assert observation.compensated_center_x is None
    assert observation.compensated_center_y is None
    assert not observation.compensation_available
