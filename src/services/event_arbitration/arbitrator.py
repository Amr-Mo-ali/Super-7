"""Small, deterministic conflict resolver for accepted event candidates."""

from collections import defaultdict
from math import isfinite

from services.event_arbitration.config import (
    DECISIVE_EVENT_EVIDENCE_MARGIN,
    MAX_END_FRAME_DIFFERENCE,
    MAX_RELEASE_FRAME_DIFFERENCE,
    MAX_START_FRAME_DIFFERENCE,
    MIN_PASS_TRAJECTORY_QUALITY,
    MIN_SHOT_TRAJECTORY_QUALITY,
    MIN_TEMPORAL_OVERLAP_RATIO,
    RELATIVE_DISTANCE_TOLERANCE,
)
from services.event_arbitration.models import (
    ArbitratedEvent,
    ArbitrationResult,
    EventCandidateRef,
    EventConflict,
)


class EventArbitrator:
    """Never mutates candidates or invokes an upstream detector."""

    def arbitrate(self, candidates: tuple[EventCandidateRef, ...]) -> ArbitrationResult:
        valid = tuple(
            candidate for candidate in candidates if candidate.end_frame >= candidate.start_frame
        )
        invalid = tuple(
            candidate for candidate in candidates if candidate.end_frame < candidate.start_frame
        )
        groups = self._groups(valid)
        events: list[ArbitratedEvent] = []
        conflicts: list[EventConflict] = []
        suppressed = 0
        ambiguous = 0
        for candidate in invalid:
            events.append(
                ArbitratedEvent(
                    f"unresolved-{candidate.event_id}",
                    None,
                    "unresolved_conflict",
                    0.0,
                    0.0,
                    candidate.start_frame,
                    candidate.release_frame,
                    candidate.end_frame,
                    (candidate.event_id,),
                    (),
                    "Candidate frame range was invalid; no public action was classified.",
                    ("invalid_frame_range", "candidate_events_are_not_confirmed_actions"),
                    (candidate.event_type,),
                )
            )
        for index, group in enumerate(groups, 1):
            if len(group) == 1:
                events.append(self._accepted(group[0]))
                continue
            event, conflict = self._resolve(group, f"conflict-{index}")
            events.append(event)
            conflicts.append(conflict)
            suppressed += len(event.suppressed_candidate_ids)
            ambiguous += event.status == "ambiguous"
        events.sort(
            key=lambda item: (
                item.start_frame,
                item.release_frame or -1,
                item.end_frame,
                item.public_event_id,
            )
        )
        return ArbitrationResult(
            tuple(events), tuple(conflicts), len(candidates), len(events), suppressed, ambiguous
        )

    def _groups(
        self, candidates: tuple[EventCandidateRef, ...]
    ) -> tuple[tuple[EventCandidateRef, ...], ...]:
        parent = list(range(len(candidates)))

        def find(value: int) -> int:
            while parent[value] != value:
                parent[value] = parent[parent[value]]
                value = parent[value]
            return value

        for left in range(len(candidates)):
            for right in range(left + 1, len(candidates)):
                if self._conflicts(candidates[left], candidates[right]):
                    a, b = find(left), find(right)
                    if a != b:
                        parent[b] = a
        grouped: dict[int, list[EventCandidateRef]] = defaultdict(list)
        for index, candidate in enumerate(candidates):
            grouped[find(index)].append(candidate)
        return tuple(
            tuple(sorted(group, key=lambda item: item.event_id)) for group in grouped.values()
        )

    def _conflicts(self, left: EventCandidateRef, right: EventCandidateRef) -> bool:
        overlap = _overlap(left, right)
        same_type = left.event_type == right.event_type
        same_release = _close(left.release_frame, right.release_frame, MAX_RELEASE_FRAME_DIFFERENCE)
        same_start = abs(left.start_frame - right.start_frame) <= MAX_START_FRAME_DIFFERENCE
        same_end = abs(left.end_frame - right.end_frame) <= MAX_END_FRAME_DIFFERENCE
        same_possessor = (
            left.possessor_track_id is not None
            and left.possessor_track_id == right.possessor_track_id
        )
        equivalent_distance = _optional_match(left.distance_pixels, right.distance_pixels)
        if same_type:
            return (
                overlap >= MIN_TEMPORAL_OVERLAP_RATIO
                and same_start
                and same_end
                and (left.release_frame is None or same_release)
                and (left.possessor_track_id is None or same_possessor)
                and equivalent_distance
            )
        if {left.event_type, right.event_type} != {"pass", "shot"}:
            return False
        return (
            overlap >= MIN_TEMPORAL_OVERLAP_RATIO
            and same_start
            and same_end
            and same_release
            and (same_type or same_possessor)
            and equivalent_distance
        )

    def _resolve(
        self, group: tuple[EventCandidateRef, ...], conflict_id: str
    ) -> tuple[ArbitratedEvent, EventConflict]:
        types = tuple(sorted({item.event_type for item in group}))
        if len(types) == 1:
            winner = max(group, key=_duplicate_key)
            event = self._accepted(
                winner,
                tuple(item.event_id for item in group if item != winner),
                "Duplicate accepted candidates were represented once.",
            )
            conflict_type = f"duplicate_{types[0]}"
        else:
            passes = tuple(item for item in group if item.event_type == "pass")
            shots = tuple(item for item in group if item.event_type == "shot")
            pass_candidate, shot_candidate = (
                max(passes, key=_duplicate_key),
                max(shots, key=_duplicate_key),
            )
            pass_strength, pass_gate = _pass_evidence(pass_candidate)
            shot_strength, shot_gate = _shot_evidence(shot_candidate)
            if pass_gate and (
                not shot_gate or pass_strength - shot_strength >= DECISIVE_EVENT_EVIDENCE_MARGIN
            ):
                event = self._accepted(
                    pass_candidate,
                    tuple(item.event_id for item in group if item != pass_candidate),
                    "Pass evidence was decisively stronger than conflicting shot evidence.",
                    abs(pass_strength - shot_strength),
                )
            elif shot_gate and (
                not pass_gate or shot_strength - pass_strength >= DECISIVE_EVENT_EVIDENCE_MARGIN
            ):
                event = self._accepted(
                    shot_candidate,
                    tuple(item.event_id for item in group if item != shot_candidate),
                    "Shot-specific evidence was decisively stronger than conflicting pass evidence.",
                    abs(pass_strength - shot_strength),
                )
            else:
                event = ArbitratedEvent(
                    conflict_id,
                    None,
                    "ambiguous",
                    max(pass_candidate.confidence, shot_candidate.confidence),
                    min(0.5, 1 - abs(pass_strength - shot_strength)),
                    min(item.start_frame for item in group),
                    pass_candidate.release_frame,
                    max(item.end_frame for item in group),
                    tuple(item.event_id for item in group),
                    (),
                    "Insufficient event-specific evidence to classify a pass/shot conflict.",
                    (
                        "pass_shot_conflict",
                        "missing_goal_geometry",
                        "candidate_events_are_not_confirmed_actions",
                    ),
                    types,
                )
            conflict_type = "pass_shot"
        first, second = group[0], group[1]
        conflict = EventConflict(
            conflict_id,
            tuple(item.event_id for item in group),
            conflict_type,
            _overlap(first, second),
            _close(first.release_frame, second.release_frame, MAX_RELEASE_FRAME_DIFFERENCE),
            first.possessor_track_id is not None
            and first.possessor_track_id == second.possessor_track_id,
            _similar_distance(first.distance_pixels, second.distance_pixels),
            event.status,
            event.explanation,
            event.limitations,
        )
        return event, conflict

    @staticmethod
    def _accepted(
        candidate: EventCandidateRef,
        suppressed: tuple[str, ...] = (),
        explanation: str = "Accepted candidate required no cross-type arbitration.",
        arbitration_confidence: float = 1.0,
    ) -> ArbitratedEvent:
        return ArbitratedEvent(
            candidate.event_id,
            candidate.event_type,
            "accepted",
            _unit(candidate.confidence),
            _unit(arbitration_confidence),
            candidate.start_frame,
            candidate.release_frame,
            candidate.end_frame,
            (candidate.event_id,),
            suppressed,
            explanation,
            ("candidate_events_are_not_confirmed_actions",),
            (candidate.event_type,),
        )


def _overlap(left: EventCandidateRef, right: EventCandidateRef) -> float:
    intersection = max(
        0, min(left.end_frame, right.end_frame) - max(left.start_frame, right.start_frame) + 1
    )
    shortest = min(left.end_frame - left.start_frame + 1, right.end_frame - right.start_frame + 1)
    return intersection / shortest if shortest > 0 else 0.0


def _close(left: int | None, right: int | None, tolerance: int) -> bool:
    return left is not None and right is not None and abs(left - right) <= tolerance


def _similar_distance(left: float | None, right: float | None) -> bool:
    return (
        left is not None
        and right is not None
        and isfinite(left)
        and isfinite(right)
        and abs(left - right) <= max(abs(left), abs(right), 1) * RELATIVE_DISTANCE_TOLERANCE
    )


def _optional_match(left: float | None, right: float | None) -> bool:
    return (left is None and right is None) or _similar_distance(left, right)


def _unit(value: float) -> float:
    return min(1.0, max(0.0, value)) if isfinite(value) else 0.0


def _duplicate_key(item: EventCandidateRef) -> tuple[float, float, int, str]:
    return (
        _unit(item.confidence),
        _unit(item.trajectory_quality or 0),
        item.end_frame - item.start_frame + 1,
        "".join(chr(255 - ord(char)) for char in item.event_id),
    )


def _pass_evidence(item: EventCandidateRef) -> tuple[float, bool]:
    quality = _unit(item.trajectory_quality or 0)
    receiver = 1.0 if item.receiver_track_id is not None else 0.0
    return 0.45 * _unit(
        item.confidence
    ) + 0.30 * quality + 0.25 * receiver, receiver == 1 and quality >= MIN_PASS_TRAJECTORY_QUALITY


def _shot_evidence(item: EventCandidateRef) -> tuple[float, bool]:
    quality = _unit(item.trajectory_quality or 0)
    signal = (
        _unit(item.preparation_confidence)
        + _unit(item.release_confidence)
        + _unit(item.follow_through_confidence)
    ) / 3
    return 0.35 * _unit(
        item.confidence
    ) + 0.30 * quality + 0.35 * signal, quality >= MIN_SHOT_TRAJECTORY_QUALITY and signal > 0
