"""Deterministic detailed ratings from already-computed analysis evidence."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Literal

from schemas.analysis import (
    PassCandidateResponse,
    PassDetectionResponse,
    PhysicalScoreResponse,
    ShotCandidateResponse,
    ShotDetectionResponse,
    TechnicalEventAnalysisResponse,
    UnsupportedMetric,
)
from services.callback_service import DetailedRatings
from services.event_arbitration.models import ArbitrationResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _EventEvidence:
    candidate_count: int
    accepted_count: int
    target_attributed_accepted_count: int
    finite_qualifying_count: int
    mean_qualifying_confidence: float | None


class DetailedRatingEngine:
    """Populate only detailed axes whose current evidence matches their narrow meaning."""

    def evaluate(
        self,
        physical: PhysicalScoreResponse | UnsupportedMetric,
        technical_events: TechnicalEventAnalysisResponse,
        technical_event_quality: float | None,
        pass_detection: PassDetectionResponse | None = None,
        shot_detection: ShotDetectionResponse | None = None,
        arbitration: ArbitrationResult | None = None,
        selected_target_track_id: int | None = None,
    ) -> DetailedRatings:
        passing, passing_evidence = self._event_score(
            pass_detection.pass_candidates if pass_detection else (),
            arbitration,
            selected_target_track_id,
            "pass",
        )
        shooting, shooting_evidence = self._event_score(
            shot_detection.shot_candidates if shot_detection else (),
            arbitration,
            selected_target_track_id,
            "shot",
        )
        self._log_event_evidence("passing", passing_evidence)
        self._log_event_evidence("shooting", shooting_evidence)
        return DetailedRatings(
            speed_and_fitness=self._visible_movement_activity(physical),
            ball_control_and_individual_skill=self._ball_control(
                technical_events, technical_event_quality
            ),
            passing_and_playmaking=passing,
            shooting_and_finishing=shooting,
        )

    @staticmethod
    def _event_score(
        candidates: Sequence[PassCandidateResponse | ShotCandidateResponse],
        arbitration: ArbitrationResult | None,
        selected_target_track_id: int | None,
        event_type: Literal["pass", "shot"],
    ) -> tuple[float | None, _EventEvidence]:
        """Score only target-attributed candidates retained by existing arbitration."""
        candidate_count = len(candidates)
        if arbitration is None or selected_target_track_id is None:
            return None, _EventEvidence(candidate_count, 0, 0, 0, None)
        accepted_ids = {
            candidate_id
            for event in arbitration.events
            if event.status == "accepted" and event.event_type == event_type
            for candidate_id in event.source_candidate_ids
        }
        accepted = [
            candidate for candidate in candidates if _candidate_id(candidate) in accepted_ids
        ]
        target_attributed = [
            candidate
            for candidate in accepted
            if candidate.possessor_track_id == selected_target_track_id
        ]
        finite = [
            candidate.confidence
            for candidate in target_attributed
            if isfinite(candidate.confidence)
        ]
        mean = sum(finite) / len(finite) if finite else None
        return (
            _score(mean * 100) if mean is not None else None,
            _EventEvidence(
                candidate_count,
                len(accepted),
                len(target_attributed),
                len(finite),
                mean,
            ),
        )

    @staticmethod
    def _log_event_evidence(axis: str, evidence: _EventEvidence) -> None:
        """Observational diagnostics must never affect a successful callback."""
        try:
            _LOGGER.info(
                "detailed_rating_evidence axis=%s candidate_count=%s accepted_count=%s "
                "target_attributed_accepted_count=%s finite_qualifying_count=%s "
                "mean_qualifying_confidence=%s",
                axis,
                evidence.candidate_count,
                evidence.accepted_count,
                evidence.target_attributed_accepted_count,
                evidence.finite_qualifying_count,
                evidence.mean_qualifying_confidence,
            )
        except Exception:
            pass

    @staticmethod
    def _visible_movement_activity(
        physical: PhysicalScoreResponse | UnsupportedMetric,
    ) -> float | None:
        """Use the existing physical evidence gate, not a physiological-fitness claim."""
        if (
            not isinstance(physical, PhysicalScoreResponse)
            or physical.status != "provisional_video_based"
            or physical.evidence is None
            or not _finite(physical.evidence.movement_intensity)
        ):
            return None
        return _score(physical.evidence.movement_intensity * 100)

    @staticmethod
    def _ball_control(
        events: TechnicalEventAnalysisResponse, technical_event_quality: float | None
    ) -> float | None:
        """Reuse the existing controlled-movement/dribble evidence calculation exactly."""
        if (
            technical_event_quality is None
            or not isfinite(technical_event_quality)
            or technical_event_quality <= 0
        ):
            return None
        controlled = [
            _controlled_component(
                item.confidence,
                item.normalized_player_displacement,
                item.direction_similarity,
                item.duration_seconds,
            )
            for item in events.controlled_movement_candidates
        ]
        dribbles = [
            _dribble_component(
                item.confidence,
                item.movement_evidence_component,
                item.proximity_persistence,
                item.path_straightness,
                item.direction_changes,
            )
            for item in events.dribble_candidates
        ]
        positive = controlled + dribbles
        if not positive:
            return None
        penalty = min(
            0.25,
            sum(item.confidence for item in events.ball_loss_candidates) / len(positive) * 0.15,
        )
        return _score((sum(positive) / len(positive) - penalty) * 100)


def _controlled_component(
    confidence: float,
    displacement: float,
    direction_similarity: float | None,
    duration_seconds: float,
) -> float:
    return (
        confidence * 0.40
        + min(1.0, displacement) * 0.25
        + max(0.0, direction_similarity or 0.0) * 0.20
        + min(1.0, duration_seconds / 2) * 0.15
    )


def _dribble_component(
    confidence: float,
    movement_evidence: float,
    proximity_persistence: float,
    path_straightness: float,
    direction_changes: int,
) -> float:
    return (
        confidence * 0.30
        + movement_evidence * 0.25
        + proximity_persistence * 0.20
        + path_straightness * 0.15
        + min(1.0, direction_changes / 3) * 0.10
    )


def _finite(value: float | None) -> bool:
    return value is not None and isfinite(value)


def _score(value: float) -> float:
    return min(100.0, max(0.0, value)) if isfinite(value) else 0.0


def _candidate_id(candidate: PassCandidateResponse | ShotCandidateResponse) -> str:
    return candidate.pass_id if isinstance(candidate, PassCandidateResponse) else candidate.shot_id
