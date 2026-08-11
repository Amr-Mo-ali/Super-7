"""Focused tests for the private production rating-evidence diagnostic."""

import logging

from _pytest.logging import LogCaptureFixture

from api.routes import _log_rating_evidence
from core.config import Settings
from services.interactions.models import InteractionAnalysisResult, InteractionDiagnostics
from services.movement.schemas import MovementMetrics, MovementPoint, MovementResult
from services.scoring.models import PhysicalScoreEvidence, PhysicalScoreResult
from services.scoring.technical import TechnicalScoreResult
from services.selection import PlayerTrack
from services.technical_events.models import TechnicalEventAnalysisResult, TechnicalEventDiagnostics


def _track() -> PlayerTrack:
    return PlayerTrack(7, 8, 10, 8, 0, 0.9, 0, True)


def _movement() -> MovementResult:
    metrics = MovementMetrics(1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0.5, 0, 0, 0, 0, 0, 0, 0)
    return MovementResult(
        metrics,
        (MovementPoint(1, 0.0, (1, 1), 1), MovementPoint(2, 2.0, (2, 2), 1)),
        1,
        3,
        2,
    )


def _physical() -> PhysicalScoreResult:
    evidence = PhysicalScoreEvidence(0.5, 0.5, 0.8, 0.8, 0.5, 0.2, 2, 2, 1)
    return PhysicalScoreResult(
        60, None, None, None, 0.7, "available", "v", None, evidence, (), "", 60, False, 0
    )


def _technical() -> TechnicalScoreResult:
    return TechnicalScoreResult(70, 0.8, "available", None, {}, None, None, 0, 0.6)


def test_rating_evidence_log_includes_available_evidence(caplog: LogCaptureFixture) -> None:
    diagnostics = InteractionDiagnostics(1, 1, 0, 0, 2, 1, 0, 0, 0, 0, 0.75, "v", 0.8, 0)
    interaction = InteractionAnalysisResult(
        (), 2, 1, 1, 0.8, 2, 2, 0.75, "v", diagnostics, (), None
    )
    events = TechnicalEventAnalysisResult((), (), (), TechnicalEventDiagnostics(), (), None)
    caplog.set_level(logging.INFO)

    _log_rating_evidence(
        logging.getLogger("rating-evidence-test"),
        "analysis-1",
        Settings(),
        _track(),
        0.6,
        interaction,
        _movement(),
        _physical(),
        events,
        _technical(),
    )

    messages = [record.getMessage() for record in caplog.records]
    message = next(message for message in messages if message.startswith("rating_evidence"))
    assert "player_track_quality=0.7200000000000001" in message
    assert "accepted_interaction_segments=1" in message
    assert "movement_duration_seconds=2.0" in message
    assert "technical_events_available=True" in message


def test_rating_evidence_log_handles_missing_optional_evidence(caplog: LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)

    _log_rating_evidence(
        logging.getLogger("rating-evidence-test"),
        "analysis-2",
        Settings(),
        _track(),
        0.0,
        None,
        None,
        None,
        None,
        TechnicalScoreResult(None, None, "unavailable", "missing", {}, None, None, 0, 0),
    )

    messages = [record.getMessage() for record in caplog.records]
    message = next(message for message in messages if "analysis_id=analysis-2" in message)
    assert "movement_available=False" in message
    assert "physical_value=None" in message
    assert "technical_events_available=False" in message
