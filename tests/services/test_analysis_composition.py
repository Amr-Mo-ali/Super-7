"""Composition-root behavior without model inference or video I/O."""

import logging

import pytest

from adapters.yolo_ball_detector import YOLOBallDetector
from adapters.yolo_player_detector import YOLOPlayerDetector
from core.config import Settings
from services.analysis_composition import create_analysis_components
from services.player_tracker import DetectionOnlyPlayerTracker, TrackingDiagnostics, TrackingRun


class _Tracker:
    model_version = "fake"

    def analyze(self, *_: object) -> TrackingRun:
        return TrackingRun((), TrackingDiagnostics(0, 0, 0, 0, 0))


def test_factory_builds_independent_lazy_default_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded: list[str] = []
    monkeypatch.setattr(YOLOPlayerDetector, "_load_model", lambda _: loaded.append("player"))
    monkeypatch.setattr(YOLOBallDetector, "_load_model", lambda _: loaded.append("ball"))
    settings = Settings(model_path="player.pt", ball_model_path="ball.pt")
    first = create_analysis_components(
        settings,
        player_detector_logger=logging.getLogger("test.player"),
        ball_detector_logger=logging.getLogger("test.ball"),
    )
    second = create_analysis_components(
        settings,
        player_detector_logger=logging.getLogger("test.player"),
        ball_detector_logger=logging.getLogger("test.ball"),
    )
    assert isinstance(first.tracker, DetectionOnlyPlayerTracker)
    assert first.tracker is not second.tracker
    assert first.extractor is not second.extractor
    assert first.physical_scorer is not second.physical_scorer
    assert first.tracker.model_version == "player.pt+ball.pt+bytetrack"
    assert loaded == []


def test_factory_honors_tracker_override() -> None:
    tracker = _Tracker()
    components = create_analysis_components(
        Settings(),
        player_detector_logger=logging.getLogger("test.player"),
        ball_detector_logger=logging.getLogger("test.ball"),
        tracker_override=tracker,
    )
    assert components.tracker is tracker
