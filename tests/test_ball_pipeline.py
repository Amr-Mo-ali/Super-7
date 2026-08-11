"""Deterministic tests for ball adapter, conservative tracking, and proximity."""

import logging

import numpy as np
import pytest

from adapters.yolo_ball_detector import YOLOBallDetector
from api.routes import _ball_quality, _validate_completed_diagnostics
from core.config import Settings
from core.exceptions import BallDetectionError, InternalDiagnosticsError
from schemas.analysis import (
    CompletedResponse,
    Diagnostics,
    FeatureMetric,
    FeaturesResponse,
    ScoresResponse,
    SelectedPlayer,
    TrackingResponse,
    UnsupportedMetric,
    VideoResponse,
)
from services.ball_detector import BallDetection
from services.ball_proximity import NormalizedBallProximityAnalyzer
from services.ball_tracker import BallTrackPoint, NearestNeighborBallTracker
from services.player_detector import BoundingBox


class _Tensor:
    def __init__(self, values: list[object]) -> None:
        self._values = values

    def cpu(self) -> "_Tensor":
        return self

    def tolist(self) -> list[object]:
        return self._values


class _Model:
    def __init__(self, boxes: list[object], confidences: list[object]) -> None:
        self._boxes, self._confidences = boxes, confidences

    def predict(self, frame: np.ndarray, **_: object) -> list[object]:
        boxes = type(
            "Boxes", (), {"xyxy": _Tensor(self._boxes), "conf": _Tensor(self._confidences)}
        )()
        return [type("Result", (), {"boxes": boxes})()]


def _ball(frame: int, x: float = 10, confidence: float = 0.9) -> BallDetection:
    box = BoundingBox(x, 10, x + 4, 14)
    return BallDetection(frame, frame / 10, confidence, box, (x + 2, 12))


def test_ball_adapter_maps_valid_detection_and_filters_invalid_box() -> None:
    detector = YOLOBallDetector(
        Settings(), logging.getLogger("test"), _Model([[1, 2, 5, 6], [3, 3, 3, 6]], [0.8, 0.9])
    )
    output = detector.detect(np.zeros((8, 8, 3), dtype=np.uint8), 4, 0.4)
    assert len(output) == 1 and output[0].center_point == (3.0, 4.0)


def test_ball_model_loading_is_deferred_until_first_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []

    def load_model(_: YOLOBallDetector) -> _Model:
        calls.append(None)
        return _Model([[1, 2, 5, 6]], [0.8])

    monkeypatch.setattr(YOLOBallDetector, "_load_model", load_model)
    detector = YOLOBallDetector(Settings(), logging.getLogger("test"))

    assert calls == []
    detector.detect(np.zeros((8, 8, 3), dtype=np.uint8), 0, 0.0)
    assert calls == [None]


def test_ball_adapter_inference_error_is_explicit() -> None:
    class Broken:
        def predict(self, *_: object, **__: object) -> list[object]:
            raise RuntimeError("boom")

    with pytest.raises(BallDetectionError):
        YOLOBallDetector(Settings(), logging.getLogger("test"), Broken()).detect(
            np.zeros((8, 8, 3), dtype=np.uint8), 0, 0
        )


def test_ball_tracker_gates_jumps_and_ends_after_long_gap() -> None:
    tracker = NearestNeighborBallTracker(Settings(ball_max_missing_frames=1, ball_motion_gate=20))
    assert tracker.update(0, 0, [_ball(0)]).visible
    assert tracker.update(1, 0.1, [_ball(1, 15)]).visible
    assert not tracker.update(2, 0.2, [_ball(2, 100)]).visible
    assert not tracker.update(3, 0.3, []).visible
    assert tracker.update(4, 0.4, [_ball(4, 100)]).segment_id == 2


def test_ball_tracker_selects_only_one_primary_candidate_per_frame() -> None:
    tracker = NearestNeighborBallTracker(Settings(ball_motion_gate=30))
    tracker.update(0, 0, [_ball(0, 10)])
    point = tracker.update(1, 0.1, [_ball(1, 12, 0.7), _ball(1, 80, 0.99)])
    assert point.visible and point.center_point == pytest.approx((13, 12))


def test_proximity_normalization_boundary_and_segments() -> None:
    analyzer = NormalizedBallProximityAnalyzer(
        Settings(ball_proximity_threshold=1.0, ball_interaction_gap_frames=1)
    )
    boxes = {
        0: BoundingBox(0, 0, 10, 10),
        1: BoundingBox(0, 0, 10, 10),
        4: BoundingBox(0, 0, 10, 10),
    }
    points = {
        0: BallTrackPoint(0, 0, (5, 10), 0.9, True, 1),
        1: BallTrackPoint(1, 0.1, (15, 10), 0.9, True, 1),
        4: BallTrackPoint(4, 0.4, (5, 10), 0.9, True, 2),
    }
    result = analyzer.analyze(boxes, points, 10)
    assert result.ball_proximity_frames == 3
    assert result.possible_ball_interaction_count == 2
    assert result.ball_proximity_time_seconds == pytest.approx(0.3)


def test_ball_quality_gate_penalizes_rejections_and_multiple_candidates() -> None:
    assert _ball_quality(100, 100, (0.9,) * 100, 1, 0, 0, 100) > 0.9
    assert _ball_quality(1, 100, (0.1,), 10, 90, 90, 100) < 0.3


def test_completed_response_rejects_contradictory_player_diagnostics() -> None:
    unavailable = UnsupportedMetric(reason="not implemented")
    response = CompletedResponse(
        analysis_id="test",
        status="completed",
        video=VideoResponse(duration_seconds=1, fps=10, width=64, height=64),
        selected_player=SelectedPlayer(
            track_id=1,
            selection_method="test",
            selection_score=1,
            confidence=1,
            visible_frames=1,
            visibility_ratio=1,
            ball_proximity_frames=0,
            ball_proximity_ratio=0,
            visibility_contribution=1,
            ball_proximity_contribution=0,
        ),
        tracking=TrackingResponse(
            frames_processed=1, lost_track_count=0, longest_continuous_visible_segment=1
        ),
        features=FeaturesResponse(
            ball_proximity_time_seconds=FeatureMetric(reason="unavailable"),
            movement_intensity=FeatureMetric(reason="unavailable"),
            direction_changes=FeatureMetric(reason="unavailable"),
        ),
        scores=ScoresResponse(
            technical=unavailable,
            physical=unavailable,
            game_intelligence=unavailable,
            mental_resilience=unavailable,
            professionalism=unavailable,
            growth_potential=unavailable,
            market_readiness=unavailable,
        ),
        diagnostics=Diagnostics(
            frames_processed=1,
            frames_with_player_detections=0,
            total_person_detections=0,
            tracks_created=0,
            valid_candidate_tracks=0,
            ball_detections=0,
        ),
        warnings=[],
        analysis_version="test",
        model_version="test",
        processing_time_ms=0,
    )
    with pytest.raises(InternalDiagnosticsError):
        _validate_completed_diagnostics(response)


def test_completed_response_rejects_contradictory_ball_diagnostics() -> None:
    unavailable = UnsupportedMetric(reason="not implemented")
    response = CompletedResponse(
        analysis_id="test",
        status="completed",
        video=VideoResponse(duration_seconds=1, fps=10, width=64, height=64),
        selected_player=SelectedPlayer(
            track_id=1,
            selection_method="test",
            selection_score=1,
            confidence=1,
            visible_frames=1,
            visibility_ratio=1,
            ball_proximity_frames=0,
            ball_proximity_ratio=0,
            visibility_contribution=1,
            ball_proximity_contribution=0,
        ),
        tracking=TrackingResponse(
            frames_processed=1, lost_track_count=0, longest_continuous_visible_segment=1
        ),
        features=FeaturesResponse(
            ball_proximity_time_seconds=FeatureMetric(reason="unavailable"),
            movement_intensity=FeatureMetric(reason="unavailable"),
            direction_changes=FeatureMetric(reason="unavailable"),
        ),
        scores=ScoresResponse(
            technical=unavailable,
            physical=unavailable,
            game_intelligence=unavailable,
            mental_resilience=unavailable,
            professionalism=unavailable,
            growth_potential=unavailable,
            market_readiness=unavailable,
        ),
        diagnostics=Diagnostics(
            frames_processed=1,
            frames_with_player_detections=1,
            total_person_detections=1,
            tracks_created=1,
            valid_candidate_tracks=1,
            ball_detections=1,
            ball_visible_frames=0,
            accepted_ball_track_observations=1,
        ),
        warnings=[],
        analysis_version="test",
        model_version="test",
        processing_time_ms=0,
    )
    with pytest.raises(InternalDiagnosticsError, match="ball visibility"):
        _validate_completed_diagnostics(response)
