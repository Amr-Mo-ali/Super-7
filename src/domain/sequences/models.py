"""Stable sequence contracts built from timeline events and transitions."""

from dataclasses import dataclass

from domain.transitions.models import EventTransition


@dataclass(frozen=True, slots=True, order=True)
class SequenceIdentifier:
    value: int

    def __post_init__(self) -> None:
        if self.value < 1:
            raise ValueError("SequenceIdentifier must be positive.")


@dataclass(frozen=True, slots=True)
class EventSequence:
    sequence_id: SequenceIdentifier
    event_ids: tuple[str, ...]
    transitions: tuple[EventTransition, ...]
    confidence: float
    evidence_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.event_ids:
            raise ValueError("EventSequence requires at least one event.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("EventSequence.confidence must be in [0, 1].")
