"""Dependency-injection contracts for technical event analysis."""

from typing import Protocol

from services.technical_events.models import TechnicalEventAnalysisResult


class TechnicalEventAnalyzerProtocol(Protocol):
    def analyze(self, *args: object, **kwargs: object) -> TechnicalEventAnalysisResult: ...


class ControlledMovementDetectorProtocol(Protocol):
    pass


class DribbleCandidateDetectorProtocol(Protocol):
    pass


class BallLossCandidateDetectorProtocol(Protocol):
    pass
