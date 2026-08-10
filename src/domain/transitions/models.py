"""Explainable links between adjacent timeline events."""

from dataclasses import dataclass
from enum import StrEnum


class TransitionStatus(StrEnum):
    SUPPORTED = "supported"
    AMBIGUOUS = "ambiguous"
    UNSUPPORTED = "unsupported"


class TransitionKind(StrEnum):
    PASS_TO_RECEPTION = "pass_to_reception"
    RECEPTION_TO_SHOT = "reception_to_shot"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EventTransition:
    transition_id: str
    source_event_id: str
    target_event_id: str
    kind: TransitionKind
    status: TransitionStatus
    confidence: float
    evidence_ids: tuple[str, ...]
    reason: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("EventTransition.confidence must be in [0, 1].")
