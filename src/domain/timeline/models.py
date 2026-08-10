"""Owned, immutable temporal event records without detector coupling."""

from dataclasses import dataclass
from typing import Literal

from domain.possession.models import PossessionState

TimelineEventType = Literal[
    "pass", "reception", "shot", "controlled_movement", "dribble", "ball_loss", "unknown"
]


@dataclass(frozen=True, slots=True)
class TemporalWindow:
    start_frame: int
    end_frame: int

    def __post_init__(self) -> None:
        if self.start_frame < 0 or self.end_frame < self.start_frame:
            raise ValueError("TemporalWindow requires non-negative inclusive frame bounds.")


@dataclass(frozen=True, slots=True)
class TimelineEvent:
    event_id: str
    event_type: TimelineEventType
    window: TemporalWindow
    confidence: float
    possession_state: PossessionState = PossessionState.UNKNOWN
    source_event_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError("TimelineEvent.event_id must not be empty.")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("TimelineEvent.confidence must be in [0, 1].")


@dataclass(frozen=True, slots=True)
class Timeline:
    """Canonical deterministic owner of ordered event records."""

    events: tuple[TimelineEvent, ...]

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(
                self.events,
                key=lambda event: (
                    event.window.start_frame,
                    event.window.end_frame,
                    event.event_id,
                ),
            )
        )
        if len({event.event_id for event in ordered}) != len(ordered):
            raise ValueError("Timeline event IDs must be unique.")
        object.__setattr__(self, "events", ordered)
