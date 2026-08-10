"""Minimal deterministic event-linking infrastructure for future consumers."""

from domain.sequences.models import EventSequence, SequenceIdentifier
from domain.timeline.models import Timeline, TimelineEvent
from domain.transitions.models import EventTransition, TransitionKind, TransitionStatus


class EventSequenceBuilder:
    """Links only pass→reception and reception→shot; it does not infer missing events."""

    def build(self, timeline: Timeline) -> tuple[EventSequence, ...]:
        sequences: list[EventSequence] = []
        for index, event in enumerate(timeline.events, 1):
            transitions: list[EventTransition] = []
            event_ids = [event.event_id]
            current = event
            for candidate in timeline.events[index:]:
                transition = self._transition(current, candidate)
                if transition.status is not TransitionStatus.SUPPORTED:
                    break
                transitions.append(transition)
                event_ids.append(candidate.event_id)
                current = candidate
            evidence = tuple(
                dict.fromkeys(
                    evidence_id
                    for sequence_event in timeline.events
                    if sequence_event.event_id in event_ids
                    for evidence_id in sequence_event.evidence_ids
                )
            )
            confidence = min(
                (event.confidence, *(transition.confidence for transition in transitions)),
                default=0.0,
            )
            sequences.append(
                EventSequence(
                    SequenceIdentifier(index),
                    tuple(event_ids),
                    tuple(transitions),
                    confidence,
                    evidence,
                )
            )
        return tuple(sequences)

    def link(self, source: TimelineEvent, target: TimelineEvent) -> EventTransition:
        """Expose a single link decision, including honest ambiguous/unsupported states."""
        return self._transition(source, target)

    @staticmethod
    def _transition(source: TimelineEvent, target: TimelineEvent) -> EventTransition:
        kind, status, reason = _rule(source, target)
        evidence = tuple(dict.fromkeys((*source.evidence_ids, *target.evidence_ids)))
        return EventTransition(
            f"{source.event_id}->{target.event_id}",
            source.event_id,
            target.event_id,
            kind,
            status,
            min(source.confidence, target.confidence),
            evidence,
            reason,
        )


def _rule(
    source: TimelineEvent, target: TimelineEvent
) -> tuple[TransitionKind, TransitionStatus, str | None]:
    if source.event_type == "pass" and target.event_type == "reception":
        return TransitionKind.PASS_TO_RECEPTION, TransitionStatus.SUPPORTED, None
    if source.event_type == "reception" and target.event_type == "shot":
        return TransitionKind.RECEPTION_TO_SHOT, TransitionStatus.SUPPORTED, None
    if source.event_type == "pass" and target.event_type == "shot":
        return TransitionKind.UNKNOWN, TransitionStatus.AMBIGUOUS, "reception_evidence_missing"
    return TransitionKind.UNKNOWN, TransitionStatus.UNSUPPORTED, "transition_not_supported_in_v0_1"
