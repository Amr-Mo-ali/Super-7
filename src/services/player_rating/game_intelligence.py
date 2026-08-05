"""Deterministic, video-only Game Intelligence V0.1 heuristic.

This module consumes compact, completed-pipeline evidence only.  It deliberately
does not inspect trajectories, frames, or invoke an analysis service.
"""

from dataclasses import dataclass
from math import isfinite

from services.player_rating.config import (
    GAME_INTELLIGENCE_WEIGHTS,
    MAX_GAME_INTELLIGENCE_CONFIDENCE,
    MIN_AVAILABLE_GAME_INTELLIGENCE_COMPONENTS,
    MIN_GAME_INTELLIGENCE_VISIBLE_DURATION_SECONDS,
    PREFERRED_GAME_INTELLIGENCE_DURATION_SECONDS,
)

VERSION = "game_intelligence_v0.1"
_LIMITATIONS = (
    "heuristic_estimation",
    "single_camera_view",
    "limited_field_of_view",
    "missing_team_context",
    "missing_opponent_context",
    "missing_phase_of_play_context",
    "candidate_events_are_not_confirmed_actions",
)


@dataclass(frozen=True, slots=True)
class GameIntelligenceEvidence:
    visible_duration_seconds: float | None
    visibility_ratio: float | None
    continuity_ratio: float | None
    ball_proximity_ratio: float | None
    interaction_time_seconds: float | None
    interaction_count: int | None
    longest_interaction_seconds: float | None
    interaction_confidence: float | None
    interaction_coverage: float | None
    ball_quality: float | None
    interaction_quality: float | None
    movement_intensity: float | None
    active_time_ratio: float | None
    direction_component: float | None
    direction_changes: float | None
    movement_quality: float | None
    technical_quality: float | None
    controlled_count: int = 0
    controlled_confidence: float | None = None
    dribble_count: int = 0
    dribble_confidence: float | None = None
    loss_count: int = 0
    loss_confidence: float | None = None
    pass_count: int = 0
    pass_confidence: float | None = None
    shot_count: int = 0
    shot_confidence: float | None = None
    technical_value: float | None = None
    technical_confidence: float | None = None
    pass_shot_overlap_count: int = 0


@dataclass(frozen=True, slots=True)
class GameIntelligenceComponent:
    name: str
    value: float | None
    confidence: float
    status: str
    explanation: str
    limitations: tuple[str, ...]
    evidence: dict[str, float | int | str]
    version: str = VERSION


@dataclass(frozen=True, slots=True)
class GameIntelligenceResult:
    value: float | None
    confidence: float
    status: str
    level: str | None
    reason: str | None
    components: tuple[GameIntelligenceComponent, ...]
    effective_weights: dict[str, float]
    available_component_count: int
    explanation: str
    limitations: tuple[str, ...]
    version: str = VERSION


class GameIntelligenceEngine:
    """An explainable indicator, not a cognitive or tactical assessment."""

    def evaluate(self, evidence: GameIntelligenceEvidence) -> GameIntelligenceResult:
        duration_seconds = evidence.visible_duration_seconds
        if (
            duration_seconds is None
            or not isfinite(duration_seconds)
            or duration_seconds < MIN_GAME_INTELLIGENCE_VISIBLE_DURATION_SECONDS
        ):
            return self._insufficient((), "insufficient_game_intelligence_evidence")
        components = (
            self._ball(evidence),
            self._decision(evidence),
            self._spatial(evidence),
            self._efficiency(evidence),
            self._technical(evidence),
        )
        available = tuple(item for item in components if item.status == "available")
        if len(available) < MIN_AVAILABLE_GAME_INTELLIGENCE_COMPONENTS:
            return self._insufficient(components, "insufficient_game_intelligence_evidence")
        total = sum(GAME_INTELLIGENCE_WEIGHTS[item.name] for item in available)
        weights = {item.name: GAME_INTELLIGENCE_WEIGHTS[item.name] / total for item in available}
        value = _score(
            sum(item.value * weights[item.name] for item in available if item.value is not None)
        )
        mean_confidence = sum(item.confidence * weights[item.name] for item in available)
        duration_factor = min(1.0, duration_seconds / PREFERRED_GAME_INTELLIGENCE_DURATION_SECONDS)
        coverage_factor = len(available) / len(GAME_INTELLIGENCE_WEIGHTS)
        # The 0.75 context factor caps certainty for absent team/opponent/phase context.
        confidence = _unit(mean_confidence * duration_factor * coverage_factor * 0.75)
        confidence = min(confidence, MAX_GAME_INTELLIGENCE_CONFIDENCE)
        limitations = _LIMITATIONS + (("short_video",) if duration_factor < 1 else ())
        if evidence.pass_shot_overlap_count:
            limitations += ("ambiguous_pass_shot_event",)
        return GameIntelligenceResult(
            value,
            confidence,
            "provisional_video_based",
            _level(value),
            None,
            components,
            weights,
            len(available),
            "Provisional video-based heuristic indicator; not a validated football-intelligence assessment.",
            limitations,
        )

    def _ball(self, e: GameIntelligenceEvidence) -> GameIntelligenceComponent:
        required = (
            e.ball_proximity_ratio,
            e.interaction_time_seconds,
            e.interaction_count,
            e.interaction_confidence,
            e.interaction_coverage,
            e.ball_quality,
            e.interaction_quality,
        )
        if (
            e.interaction_count is None
            or e.ball_quality is None
            or e.interaction_quality is None
            or not all(_finite_number(x) for x in required)
            or e.interaction_count <= 0
            or e.ball_quality < 0.45
            or e.interaction_quality < 0.45
        ):
            return _unavailable("ball_involvement", "insufficient_ball_interaction_evidence")
        assert e.interaction_time_seconds is not None
        duration = min(1.0, e.interaction_time_seconds / 5.0)
        frequency = min(1.0, e.interaction_count / 4.0)
        longest = min(1.0, (e.longest_interaction_seconds or 0.0) / 3.0)
        value = _score(
            100
            * (
                0.35 * _unit(e.ball_proximity_ratio)
                + 0.25 * duration
                + 0.20 * frequency
                + 0.10 * longest
                + 0.10 * _unit(e.interaction_confidence)
            )
        )
        confidence = _unit(
            (
                _unit(e.interaction_confidence)
                + _unit(e.interaction_coverage)
                + _unit(e.ball_quality)
                + _unit(e.interaction_quality)
            )
            / 4
        )
        return _component(
            "ball_involvement",
            value,
            confidence,
            "Observed proximity and possible interaction consistency.",
            {
                "interaction_count": e.interaction_count,
                "interaction_time_seconds": e.interaction_time_seconds,
            },
        )

    def _decision(self, e: GameIntelligenceEvidence) -> GameIntelligenceComponent:
        positives = e.controlled_count + e.pass_count + e.shot_count - e.pass_shot_overlap_count
        if positives <= 0 and e.technical_value is None:
            return _unavailable("decision_consistency", "insufficient_technical_event_evidence")
        quality = _unit(e.technical_quality)
        if quality == 0:
            return _unavailable("decision_consistency", "insufficient_technical_event_evidence")
        pos_conf = _mean(
            e.controlled_confidence, e.pass_confidence, e.shot_confidence, e.technical_confidence
        )
        loss = min(1.0, e.loss_count / max(positives + e.loss_count, 1)) * _unit(e.loss_confidence)
        base = (
            _unit(e.technical_value / 100)
            if e.technical_value is not None and isfinite(e.technical_value)
            else min(1.0, positives / 4) * pos_conf
        )
        value = _score(100 * _unit(base * (1 - 0.35 * loss)))
        confidence = _unit(quality * pos_conf * (0.8 if e.pass_shot_overlap_count else 1.0))
        limitations = (
            ("pass_shot_candidate_overlap_not_arbitrated",) if e.pass_shot_overlap_count else ()
        )
        return _component(
            "decision_consistency",
            value,
            confidence,
            "Observable candidate-event consistency proxy, not decision quality.",
            {"positive_event_count": positives, "ball_loss_count": e.loss_count},
            limitations,
        )

    def _spatial(self, e: GameIntelligenceEvidence) -> GameIntelligenceComponent:
        if (
            e.movement_quality is None
            or e.visible_duration_seconds is None
            or not all(
                _finite(x)
                for x in (
                    e.movement_intensity,
                    e.active_time_ratio,
                    e.direction_component,
                    e.movement_quality,
                    e.continuity_ratio,
                    e.visible_duration_seconds,
                )
            )
            or e.movement_quality < 0.55
        ):
            return _unavailable("spatial_activity_proxy", "insufficient_movement_evidence")
        direction_rate = min(
            1.0, max(0.0, e.direction_changes or 0.0) / max(e.visible_duration_seconds, 1.0) / 0.5
        )
        value = _score(
            100
            * (
                0.35 * _unit(e.movement_intensity)
                + 0.25 * _unit(e.active_time_ratio)
                + 0.20 * _unit(e.direction_component)
                + 0.20 * direction_rate
            )
        )
        confidence = _unit(
            (_unit(e.movement_quality) + _unit(e.continuity_ratio) + _unit(e.visibility_ratio)) / 3
        )
        return _component(
            "spatial_activity_proxy",
            value,
            confidence,
            "Image-space activity proxy; not tactical positioning or spatial awareness.",
            {
                "direction_changes_per_second": (e.direction_changes or 0.0)
                / e.visible_duration_seconds
            },
        )

    def _efficiency(self, e: GameIntelligenceEvidence) -> GameIntelligenceComponent:
        if (
            e.movement_quality is None
            or not all(
                _finite(x)
                for x in (
                    e.movement_intensity,
                    e.active_time_ratio,
                    e.direction_component,
                    e.movement_quality,
                    e.continuity_ratio,
                )
            )
            or e.movement_quality < 0.55
        ):
            return _unavailable("movement_efficiency_proxy", "insufficient_movement_evidence")
        value = _score(
            100
            * (
                0.35 * _unit(e.movement_intensity)
                + 0.25 * _unit(e.direction_component)
                + 0.20 * _unit(e.active_time_ratio)
                + 0.20 * _unit(e.continuity_ratio)
            )
        )
        confidence = _unit(
            (_unit(e.movement_quality) + _unit(e.continuity_ratio) + _unit(e.visibility_ratio)) / 3
        )
        return _component(
            "movement_efficiency_proxy",
            value,
            confidence,
            "Normalized image-space movement proxy; not speed, fitness, or stamina.",
            {},
        )

    def _technical(self, e: GameIntelligenceEvidence) -> GameIntelligenceComponent:
        if not _finite(e.technical_value) or not _finite(e.technical_confidence):
            return _unavailable("technical_involvement", "insufficient_technical_evidence")
        assert e.technical_value is not None
        assert e.technical_confidence is not None
        return _component(
            "technical_involvement",
            _score(e.technical_value),
            _unit(e.technical_confidence) * _unit(e.technical_quality),
            "Adapts the existing technical candidate-event score.",
            {
                "controlled_count": e.controlled_count,
                "dribble_count": e.dribble_count,
                "pass_count": e.pass_count,
                "shot_count": e.shot_count,
                "ball_loss_count": e.loss_count,
            },
        )

    @staticmethod
    def _insufficient(
        components: tuple[GameIntelligenceComponent, ...], reason: str
    ) -> GameIntelligenceResult:
        return GameIntelligenceResult(
            None,
            0.0,
            "insufficient_evidence",
            None,
            reason,
            components,
            {},
            sum(x.status == "available" for x in components),
            "A numeric provisional indicator is unavailable because the evidence gate was not met.",
            _LIMITATIONS,
            VERSION,
        )


def _component(
    name: str,
    value: float,
    confidence: float,
    explanation: str,
    evidence: dict[str, float | int | str],
    limitations: tuple[str, ...] = (),
) -> GameIntelligenceComponent:
    return GameIntelligenceComponent(
        name,
        _score(value),
        _unit(confidence),
        "available",
        explanation,
        limitations + _LIMITATIONS,
        evidence,
    )


def _unavailable(name: str, reason: str) -> GameIntelligenceComponent:
    return GameIntelligenceComponent(
        name,
        None,
        0.0,
        "insufficient_evidence",
        "A numeric component is unavailable because its evidence gate was not met.",
        (reason,),
        {"reason": reason},
    )


def _finite(value: float | None) -> bool:
    return value is not None and isfinite(value)


def _finite_number(value: float | int | None) -> bool:
    return value is not None and isfinite(value)


def _unit(value: float | None) -> float:
    if not _finite(value):
        return 0.0
    assert value is not None
    return min(1.0, max(0.0, value))


def _score(value: float) -> float:
    return min(100.0, max(0.0, value)) if isfinite(value) else 0.0


def _mean(*values: float | None) -> float:
    present = [_unit(x) for x in values if _finite(x)]
    return sum(present) / len(present) if present else 0.0


def _level(value: float) -> str:
    return (
        "very_low"
        if value < 20
        else "low"
        if value < 35
        else "developing"
        if value < 50
        else "moderate"
        if value < 65
        else "good"
        if value < 80
        else "very_good"
        if value < 90
        else "excellent"
    )
