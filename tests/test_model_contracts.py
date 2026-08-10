"""Serialization and immutability contracts for public and domain models."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from schemas.analysis import PassCandidateResponse, StageGate
from services.player_detector import BoundingBox


def test_domain_bounding_box_is_immutable() -> None:
    box = BoundingBox(1, 2, 3, 4)

    with pytest.raises(FrozenInstanceError):
        box.x1 = 0  # type: ignore[misc]


def test_public_pass_candidate_serializes_coordinates_as_json_arrays() -> None:
    response = PassCandidateResponse(
        pass_id="pass-1",
        possessor_track_id=1,
        receiver_track_id=2,
        start_frame=10,
        release_frame=12,
        end_frame=20,
        duration_seconds=1.0,
        distance=100.0,
        confidence=0.8,
        release_speed=50.0,
        trajectory_points=[(1.0, 2.0), (3.0, 4.0)],
        trajectory_duration=0.8,
        trajectory_length=100.0,
        trajectory_direction=(1.0, 0.0),
        trajectory_quality=0.7,
        status="pass_candidate",
    )

    payload = response.model_dump(mode="json")

    assert payload["trajectory_points"] == [[1.0, 2.0], [3.0, 4.0]]
    assert payload["trajectory_direction"] == [1.0, 0.0]


def test_quality_gate_rejects_out_of_range_quality() -> None:
    with pytest.raises(ValidationError):
        StageGate(quality=1.1, status="accepted")
