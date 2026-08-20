"""Focused coverage for the completed-analysis evidence diagnostic event."""

import logging

from _pytest.logging import LogCaptureFixture
from application_log_capture import capture_application_logs
from pytest import MonkeyPatch

from api.routes import (
    _log_interaction_evidence,
    _log_rating_evidence,
    _log_target_track_candidates,
    _log_target_track_evidence,
)
from core.config import Settings
from core.logging import configure_logging
from services.interactions.models import InteractionAnalysisResult, InteractionDiagnostics
from services.pass_detection import PassDetectionResult
from services.player_detector import BoundingBox
from services.player_rating.game_intelligence import (
    GameIntelligenceEngine,
    GameIntelligenceEvidence,
    GameIntelligenceResult,
)
from services.player_tracker import TrackingDiagnostics, TrackingRun
from services.scoring.models import PhysicalEvidenceDiagnostics, PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult
from services.segment_selection import TrackSegment, rank_segments, select_segment
from services.selection import PlayerTrack, Selection, WeightedTargetPlayerSelector
from services.shot_detection import ShotDetectionResult
from services.technical_events.models import (
    TechnicalEventAnalysisResult,
    TechnicalEventDiagnostics,
    TechnicalEvidenceDiagnostics,
)
from services.video_validator import VideoMetadata


def _technical_events() -> TechnicalEventAnalysisResult:
    gate = TechnicalEvidenceDiagnostics(
        0.4,
        0.6,
        0.3,
        0.5,
        {
            "player_track_quality": 0.5,
            "ball_analysis_quality": 0.5,
            "interaction_analysis_quality": 0.5,
            "interaction_evidence_coverage_ratio": 0.6,
        },
        (
            "player_track_quality",
            "interaction_analysis_quality",
            "interaction_evidence_coverage_ratio",
        ),
    )
    return TechnicalEventAnalysisResult(
        (), (), (), TechnicalEventDiagnostics(evidence_gate=gate), (), "x"
    )


def _physical() -> PhysicalScoreResult:
    gate = PhysicalEvidenceDiagnostics(
        0.4,
        0.8,
        2.0,
        20,
        0.4,
        {
            "movement_quality": 0.55,
            "visibility_ratio": 0.2,
            "visible_duration_seconds": 3.0,
            "movement_observations": 30,
            "accepted_interval_ratio": 0.6,
        },
        (
            "movement_quality",
            "visible_duration_seconds",
            "movement_observations",
            "accepted_interval_ratio",
        ),
    )
    return PhysicalScoreResult(
        None,
        None,
        None,
        None,
        None,
        "insufficient_evidence",
        "v",
        "x",
        None,
        (),
        "",
        None,
        False,
        0,
        gate,
    )


def _game() -> GameIntelligenceResult:
    return GameIntelligenceEngine().evaluate(
        GameIntelligenceEvidence(
            3.0,
            0.8,
            0.8,
            0.8,
            1.0,
            1,
            1.0,
            0.8,
            0.8,
            0.8,
            0.8,
            0.8,
            0.8,
            0.8,
            1.0,
            0.8,
            0.8,
        )
    )


def _interaction() -> InteractionAnalysisResult:
    diagnostics = InteractionDiagnostics(3, 2, 0, 0, 2, 1, 0, 0, 0, 0, 0.8, "v", 0.8, 0)
    return InteractionAnalysisResult((), 3, 1.0, 1.0, 0.8, 3, 3, 0.8, "v", diagnostics, (), None)


def test_rating_evidence_log_identifies_all_failed_gates_and_analysis_id(
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="football_analysis")
    with capture_application_logs(caplog):
        _log_rating_evidence(
            logging.getLogger("football_analysis.api"),
            "analysis-1",
            _physical(),
            _technical_events(),
            TechnicalScoreResult(None, None, "unavailable", "x", {}, None, None, 0, 0),
            _game(),
            _interaction(),
            PassDetectionResult((), 3, 1, 2, {}, 0),
            ShotDetectionResult((), 2, 1, 1, {}, 0),
        )

    message = next(
        record.getMessage() for record in caplog.records if record.msg.startswith("rating_evidence")
    )
    assert "analysis_id=analysis-1" in message
    assert (
        "technical_failed_reasons=('player_track_quality', 'interaction_analysis_quality', 'interaction_evidence_coverage_ratio')"
        in message
    )
    assert (
        "physical_failed_reasons=('movement_quality', 'visible_duration_seconds', 'movement_observations', 'accepted_interval_ratio')"
        in message
    )
    assert "game_failed_reasons=('visible_duration_seconds',)" in message
    assert (
        "possible_ball_interactions=3 interaction_segments=0 accepted_interaction_segments=1"
        in message
    )
    assert (
        "pass_detection_available=True pass_candidates=3 accepted_passes=1 "
        "shot_detection_available=True shot_candidates=2 accepted_shots=1" in message
    )


def test_rating_evidence_logger_is_reachable_from_production_namespace() -> None:
    configure_logging()
    assert logging.getLogger("football_analysis.api").isEnabledFor(logging.INFO)


def test_interaction_evidence_log_uses_existing_counters_and_is_best_effort(
    caplog: LogCaptureFixture, monkeypatch: MonkeyPatch
) -> None:
    logger = logging.getLogger("football_analysis.interaction-test")
    caplog.set_level(logging.WARNING, logger="football_analysis")
    selection = Selection(PlayerTrack(7, 8, 10, 8, 0, 0.9, 0, True), "test", 0.0, 0.0, 0.0)
    metadata = VideoMetadata("mp4", 1, 1.0, 10, 10, 10.0, 10)

    with capture_application_logs(caplog):
        _log_interaction_evidence(
            logger, "analysis-2", selection, metadata, 8, 6, 0.72, 0.8, _interaction()
        )

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.msg.startswith("interaction_evidence")
    )
    assert "analysis_id=analysis-2 track_id=7 player_observation_count=8" in message
    assert "aligned_player_ball_evidence_frames=3" in message
    assert "proximity_qualified_frames=2 proximity_ratio=0.6666666666666666" in message
    assert (
        "rejection_counts={'short': 0, 'low_confidence': 0, 'low_global_quality': 0, 'invalid': 0}"
        in message
    )

    def fail_warning(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated logging failure")

    monkeypatch.setattr(logger, "warning", fail_warning)
    _log_interaction_evidence(
        logger, "analysis-3", selection, metadata, 8, 6, 0.72, 0.8, _interaction()
    )


def test_target_track_evidence_logs_raw_continuity_without_mutating_selection(
    caplog: LogCaptureFixture,
) -> None:
    logger = logging.getLogger("football_analysis.target-track-test")
    caplog.set_level(logging.WARNING, logger="football_analysis")
    selection = Selection(
        PlayerTrack(7, 3, 3, 3, 0, 0.9, 0, True),
        "best_continuous_track_segment",
        0.0,
        0.0,
        0.0,
        1,
        0,
        2,
        0.3,
    )
    run = TrackingRun(
        (selection.track,),
        TrackingDiagnostics(10, 3, 3, 1, 0),
        {7: {0: _box(), 1: _box(), 4: _box()}},
        {7: {0: 0.8, 1: 0.9, 4: 1.0}},
    )
    metadata = VideoMetadata("mp4", 1, 1.0, 10, 10, 10.0, 10)
    selector = WeightedTargetPlayerSelector(Settings(selection_margin=0.01))
    before = selector.select(run.tracks)

    with capture_application_logs(caplog):
        _log_target_track_evidence(logger, "analysis-4", selection, metadata, run)

    after = selector.select(run.tracks)

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.msg.startswith("target_track_evidence")
    )
    assert "analysis_id=analysis-4 track_id=7" in message
    assert "visible_duration_seconds=0.3 visibility_ratio=0.3 observation_count=3" in message
    assert "longest_continuous_segment_seconds=0.2 track_fragment_count=2" in message
    assert "gap_count=1 largest_gap_seconds=0.2 average_confidence=0.9" in message
    assert "tracking_quality=0.9 candidate_track_count=1" in message
    assert (
        "selection_method=best_continuous_track_segment selected_segment_observation_count=3 "
        "selected_segment_duration_seconds=0.3" in message
    )
    assert (
        "selected_identity_continues_under_another_track_id=unknown_no_reidentification_history"
        in message
    )
    assert (
        "suspected_id_switch_indicators=() fragmentation_indicators=('observation_gaps_present',)"
        in message
    )
    assert selection.track.visible_frames == 3
    assert run.player_boxes is not None and sorted(run.player_boxes[7]) == [0, 1, 4]
    assert after == before


def test_target_track_candidates_log_existing_segment_ranking_without_mutation(
    caplog: LogCaptureFixture, monkeypatch: MonkeyPatch
) -> None:
    logger = logging.getLogger("football_analysis.target-candidates-test")
    caplog.set_level(logging.WARNING, logger="football_analysis")
    segments = (
        _segment(track_id=7, segment_id=1, quality=0.93),
        _segment(track_id=8, segment_id=1, quality=0.8),
        _segment(track_id=9, segment_id=1, quality=0.99, reasons=("low_segment_quality",)),
    )
    ranked = rank_segments(segments)
    selected = select_segment(segments)
    assert selected is not None
    tracks = tuple(PlayerTrack(segment.track_id, 3, 3, 3, 0, 0.9, 0, True) for segment in segments)
    run = TrackingRun(
        tracks,
        TrackingDiagnostics(10, 9, 9, 3, 0),
        {track.track_id: {0: _box(), 1: _box(), 2: _box()} for track in tracks},
        {track.track_id: {0: 0.9, 1: 0.9, 2: 0.9} for track in tracks},
    )
    metadata = VideoMetadata("mp4", 1, 1.0, 10, 10, 10.0, 10)

    with capture_application_logs(caplog):
        _log_target_track_candidates(
            logger, "analysis-5", selected, (selected,), metadata, run, Settings(), segments, ranked
        )

    message = next(
        record.getMessage()
        for record in caplog.records
        if record.msg.startswith("target_track_candidates")
    )
    assert "analysis_id=analysis-5 selected_track_id=7 ranking_mode=continuous_segment" in message
    assert "eligible_segment_count=2 rejected_segment_count=1" in message
    assert "'rank': 1, 'track_id': 7" in message
    assert "'selection_score': 0.93" in message
    assert "'ranking_components':" in message
    assert "'is_selected': True" in message
    assert "'rank': 2, 'track_id': 8" in message
    assert select_segment(segments) == selected
    assert rank_segments(segments) == ranked
    assert run.player_boxes is not None and sorted(run.player_boxes[7]) == [0, 1, 2]

    def fail_warning(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("simulated logging failure")

    monkeypatch.setattr(logger, "warning", fail_warning)
    _log_target_track_candidates(
        logger, "analysis-6", selected, (selected,), metadata, run, Settings(), segments, ranked
    )


def _segment(
    track_id: int,
    segment_id: int,
    quality: float,
    reasons: tuple[str, ...] = (),
) -> TrackSegment:
    return TrackSegment(
        track_id,
        segment_id,
        0,
        2,
        0.3,
        3,
        1.0,
        0.9,
        100.0,
        100.0,
        0.0,
        0,
        0.0,
        quality,
        reasons,
    )


def _box() -> BoundingBox:
    return BoundingBox(0, 0, 10, 10)
