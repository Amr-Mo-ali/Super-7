"""Evidence-based technical score from existing detected football-event candidates."""

from dataclasses import dataclass

from services.technical_events.models import (
    ControlledMovementCandidate,
    DribbleCandidate,
    TechnicalEventAnalysisResult,
)

VERSION = "technical_scoring_v0.1"


@dataclass(frozen=True, slots=True)
class TechnicalScoreResult:
    value: float | None
    confidence: float | None
    status: str
    reason: str | None
    evidence: dict[str, float]
    controlled_component: float | None
    dribble_component: float | None
    ball_loss_penalty: float
    quality: float


class TechnicalScorer:
    """Scores only available technical evidence; missing event types are neutral."""

    def score(self, events: TechnicalEventAnalysisResult | None) -> TechnicalScoreResult:
        if events is None:
            return self._unavailable("Technical-event analysis was unavailable.")
        controlled = events.controlled_movement_candidates
        dribbles = events.dribble_candidates
        losses = events.ball_loss_candidates
        if not controlled and not dribbles:
            return self._unavailable(
                "No sufficient controlled-movement or dribble evidence was detected."
            )
        controlled_score = self._controlled(controlled) if controlled else None
        dribble_score = self._dribble(dribbles) if dribbles else None
        positive = [value for value in (controlled_score, dribble_score) if value is not None]
        base = sum(positive) / len(positive)
        penalty = min(0.25, sum(item.confidence for item in losses) / max(len(positive), 1) * 0.15)
        quality = events.diagnostics.technical_event_analysis_quality
        value = max(0.0, min(100.0, (base - penalty) * 100))
        confidence = max(
            0.0,
            min(
                1.0,
                quality
                * (
                    sum(item.confidence for item in controlled + dribbles)
                    / (len(controlled) + len(dribbles))
                ),
            ),
        )
        return TechnicalScoreResult(
            value,
            confidence,
            "provisional_event_based",
            None,
            {
                "controlled_movement_events": float(len(controlled)),
                "dribble_events": float(len(dribbles)),
                "ball_loss_events": float(len(losses)),
            },
            controlled_score,
            dribble_score,
            penalty,
            quality,
        )

    @staticmethod
    def _controlled(events: tuple[ControlledMovementCandidate, ...]) -> float:
        values = []
        for event in events:
            values.append(
                event.confidence * 0.40
                + min(1.0, event.normalized_player_displacement) * 0.25
                + max(0.0, event.direction_similarity or 0.0) * 0.20
                + min(1.0, event.duration_seconds / 2) * 0.15
            )
        return sum(values) / len(values)

    @staticmethod
    def _dribble(events: tuple[DribbleCandidate, ...]) -> float:
        values = []
        for event in events:
            values.append(
                event.confidence * 0.30
                + event.movement_evidence_component * 0.25
                + event.proximity_persistence * 0.20
                + event.path_straightness * 0.15
                + min(1.0, event.direction_changes / 3) * 0.10
            )
        return sum(values) / len(values)

    @staticmethod
    def _unavailable(reason: str) -> TechnicalScoreResult:
        return TechnicalScoreResult(None, None, "unavailable", reason, {}, None, None, 0.0, 0.0)
