"""Deterministic Event Arbitration V0.1 unit coverage."""

from enum import Enum

import pytest

from services.event_arbitration import EventArbitrator, EventCandidateRef
from services.event_arbitration.models import EventType


class _DefaultReceiver(Enum):
    VALUE = "value"


def _candidate(
    event_id: str,
    event_type: EventType = "pass",
    *,
    start_frame: int = 10,
    release_frame: int | None = 12,
    end_frame: int = 20,
    possessor_track_id: int | None = 7,
    receiver_track_id: int | None | _DefaultReceiver = _DefaultReceiver.VALUE,
    confidence: float = 0.8,
    trajectory_quality: float | None = 0.8,
    distance_pixels: float | None = 100,
    source_version: str = "v",
    preparation_confidence: float | None = None,
    release_confidence: float | None = None,
    follow_through_confidence: float | None = None,
) -> EventCandidateRef:
    resolved_receiver_track_id: int | None
    if receiver_track_id is _DefaultReceiver.VALUE:
        resolved_receiver_track_id = 9 if event_type == "pass" else None
    else:
        resolved_receiver_track_id = receiver_track_id
    return EventCandidateRef(
        event_id,
        event_type,
        start_frame,
        release_frame,
        end_frame,
        possessor_track_id,
        resolved_receiver_track_id,
        confidence,
        trajectory_quality,
        distance_pixels,
        source_version,
        0.8 if event_type == "shot" else preparation_confidence,
        0.8 if event_type == "shot" else release_confidence,
        0.8 if event_type == "shot" else follow_through_confidence,
    )


def test_empty_single_and_non_overlapping_candidates() -> None:
    engine = EventArbitrator()
    assert engine.arbitrate(()).public_event_count == 0
    assert engine.arbitrate((_candidate("p1"),)).events[0].event_type == "pass"
    result = engine.arbitrate(
        (_candidate("p1"), _candidate("s1", "shot", start_frame=21, release_frame=22, end_frame=30))
    )
    assert result.public_event_count == 2


def test_duplicates_choose_confidence_quality_then_stable_id() -> None:
    engine = EventArbitrator()
    result = engine.arbitrate((_candidate("z"), _candidate("a", confidence=0.9)))
    assert result.events[0].public_event_id == "a" and result.suppressed_duplicate_count == 1
    result = engine.arbitrate(
        (
            _candidate("z", confidence=0.8, trajectory_quality=0.9),
            _candidate("a", confidence=0.8, trajectory_quality=0.8),
        )
    )
    assert result.events[0].public_event_id == "z"
    result = engine.arbitrate((_candidate("z"), _candidate("a")))
    assert result.events[0].public_event_id == "a"


def test_overlap_and_release_boundaries_are_inclusive() -> None:
    engine = EventArbitrator()
    assert (
        engine.arbitrate(
            (_candidate("a"), _candidate("b", start_frame=12, release_frame=13, end_frame=22))
        ).public_event_count
        == 1
    )
    assert (
        engine.arbitrate((_candidate("a"), _candidate("b", release_frame=14))).public_event_count
        == 2
    )
    assert (
        engine.arbitrate(
            (_candidate("a"), _candidate("b", start_frame=21, release_frame=22, end_frame=30))
        ).public_event_count
        == 2
    )


def test_pass_shot_selects_pass_when_receiver_evidence_is_stronger() -> None:
    result = EventArbitrator().arbitrate(
        (_candidate("p"), _candidate("s", "shot", trajectory_quality=0.2, confidence=0.95))
    )
    assert result.events[0].event_type == "pass"
    assert result.events[0].source_candidate_ids == ("p",)
    assert result.events[0].suppressed_candidate_ids == ("s",)


def test_pass_shot_selects_shot_or_preserves_ambiguity() -> None:
    shot = _candidate("s", "shot", confidence=0.95, trajectory_quality=0.9)
    weak_pass = _candidate("p", receiver_track_id=None, trajectory_quality=0.2)
    assert EventArbitrator().arbitrate((weak_pass, shot)).events[0].event_type == "shot"
    ambiguous_pass = _candidate("p", confidence=0.8, trajectory_quality=0.8)
    ambiguous_shot = _candidate("s", "shot", confidence=0.8, trajectory_quality=0.8)
    event = EventArbitrator().arbitrate((ambiguous_pass, ambiguous_shot)).events[0]
    assert (
        event.status == "ambiguous"
        and event.event_type is None
        and event.arbitration_confidence <= 0.5
    )


def test_inputs_are_immutable_invalid_ranges_are_explicit_and_results_are_stable() -> None:
    candidate = _candidate("p")
    engine = EventArbitrator()
    result = engine.arbitrate((candidate, _candidate("bad", end_frame=9)))
    assert result.raw_candidate_count == 2
    assert any(item.status == "unresolved_conflict" for item in result.events)
    assert candidate == _candidate("p")
    assert engine.arbitrate((candidate,)) == engine.arbitrate((candidate,))


@pytest.mark.parametrize("confidence", [float("nan"), -1.0, 2.0])
def test_non_finite_or_out_of_range_confidence_is_safe(confidence: float) -> None:
    event = EventArbitrator().arbitrate((_candidate("p", confidence=confidence),)).events[0]
    assert 0 <= event.event_confidence <= 1
