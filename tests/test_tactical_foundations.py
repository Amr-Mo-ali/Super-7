"""Pure-domain tests for Phase 4 tactical intelligence foundations."""

import pytest

from domain.possession import PossessionState
from domain.sequences import EventSequenceBuilder, SequenceIdentifier
from domain.timeline import TemporalWindow, Timeline, TimelineEvent
from domain.transitions import TransitionKind, TransitionStatus


def _event(event_id: str, event_type: str, start: int, confidence: float = 0.8) -> TimelineEvent:
    return TimelineEvent(
        event_id,
        event_type,  # type: ignore[arg-type]
        TemporalWindow(start, start + 2),
        confidence,
        PossessionState.PLAYER_CONTROLLED,
        (f"source-{event_id}",),
        (f"evidence-{event_id}",),
    )


def test_timeline_has_deterministic_ownership_and_ordering() -> None:
    timeline = Timeline((_event("shot", "shot", 20), _event("pass", "pass", 1)))
    assert tuple(event.event_id for event in timeline.events) == ("pass", "shot")
    with pytest.raises(ValueError, match="unique"):
        Timeline((_event("same", "pass", 1), _event("same", "shot", 3)))


def test_pass_reception_shot_creates_stable_sequence_and_propagates_confidence() -> None:
    timeline = Timeline(
        (
            _event("pass", "pass", 1, 0.9),
            _event("reception", "reception", 5, 0.7),
            _event("shot", "shot", 9, 0.8),
        )
    )
    sequence = EventSequenceBuilder().build(timeline)[0]
    assert sequence.sequence_id == SequenceIdentifier(1)
    assert sequence.event_ids == ("pass", "reception", "shot")
    assert tuple(item.kind for item in sequence.transitions) == (
        TransitionKind.PASS_TO_RECEPTION,
        TransitionKind.RECEPTION_TO_SHOT,
    )
    assert sequence.confidence == 0.7
    assert sequence.evidence_ids == ("evidence-pass", "evidence-reception", "evidence-shot")


def test_ambiguous_and_unsupported_transitions_remain_explicit() -> None:
    builder = EventSequenceBuilder()
    ambiguous = builder.link(_event("pass", "pass", 1), _event("shot", "shot", 5))
    unsupported = builder.link(_event("dribble", "dribble", 1), _event("shot", "shot", 5))
    assert ambiguous.status is TransitionStatus.AMBIGUOUS
    assert ambiguous.reason == "reception_evidence_missing"
    assert unsupported.status is TransitionStatus.UNSUPPORTED
    assert unsupported.kind is TransitionKind.UNKNOWN


def test_window_and_sequence_identifier_validate_invariants() -> None:
    with pytest.raises(ValueError, match="inclusive"):
        TemporalWindow(4, 3)
    with pytest.raises(ValueError, match="positive"):
        SequenceIdentifier(0)
