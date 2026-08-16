"""O(n), deterministic candidate detection using existing tracking evidence only."""

import logging
from math import acos, degrees, hypot
from time import perf_counter
from typing import Literal

from core.config import Settings
from core.exceptions import InternalTechnicalEventDiagnosticsError, TechnicalEventInputError
from services.interactions.models import (
    BallObservation,
    InteractionAnalysisResult,
    PlayerObservation,
)
from services.movement.schemas import MovementResult
from services.technical_events.models import (
    BallLossCandidate,
    ControlledMovementCandidate,
    DribbleCandidate,
    TechnicalEventAnalysisResult,
    TechnicalEventDiagnostics,
    TechnicalEvidenceDiagnostics,
)

CONTROLLED_VERSION = "controlled_movement_confidence_v0.1"
DRIBBLE_VERSION = "dribble_candidate_confidence_v0.2"
BALL_LOSS_VERSION = "ball_loss_candidate_confidence_v0.1"
_LOGGER = logging.getLogger(__name__)


class TechnicalEventAnalyzer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(
        self,
        players: tuple[PlayerObservation, ...],
        balls: tuple[BallObservation, ...],
        interactions: InteractionAnalysisResult,
        movement: MovementResult | None,
        fps: float,
        frame_dimensions: tuple[int, int],
        player_track_quality: float,
        ball_analysis_quality: float,
        interaction_analysis_quality: float,
    ) -> TechnicalEventAnalysisResult:
        del frame_dimensions
        started = perf_counter()
        if fps <= 0:
            raise TechnicalEventInputError("Technical-event analysis requires positive FPS.")
        quality = min(player_track_quality, ball_analysis_quality, interaction_analysis_quality)
        evidence_gate = self._evidence_gate(
            player_track_quality,
            ball_analysis_quality,
            interaction_analysis_quality,
            interactions.interaction_evidence_coverage_ratio,
        )
        warning = (
            "Technical events are heuristic candidates and do not prove confirmed football actions."
        )
        if evidence_gate.failed_reasons:
            return self._result(
                (),
                (),
                (),
                TechnicalEventDiagnostics(
                    technical_event_analysis_quality=quality,
                    processing_time_ms=round((perf_counter() - started) * 1000),
                    controlled_movement_rejection_breakdown={
                        "duration": 0,
                        "displacement": 0,
                        "proximity": 0,
                        "direction": 0,
                        "coverage": 0,
                        "confidence": 0,
                    },
                    controlled_movement_thresholds=self._controlled_thresholds(),
                    evidence_gate=evidence_gate,
                ),
                (
                    warning,
                    "Technical-event evidence was insufficient for reliable candidate detection.",
                ),
                "Technical-event evidence was insufficient for reliable candidate detection.",
            )
        player = {item.frame_index: item for item in players}
        ball = {item.frame_index: item for item in balls}
        if len(player) != len(players) or len(ball) != len(balls):
            raise TechnicalEventInputError(
                "Duplicate technical-event observation frames are not allowed."
            )
        controlled, cs, cr_short, cr_conf, breakdown, statistics = self._controlled(
            interactions, player, ball, quality
        )
        dribbles, ds, dr_move, dr_conf, dribble_stats, dribble_breakdown = self._dribbles(
            controlled, player, ball, movement, quality
        )
        losses, ls, lr_missing, lr_recovery = self._losses(interactions, player, ball, quality, fps)
        diagnostics = TechnicalEventDiagnostics(
            cs,
            len(controlled),
            cr_short,
            cr_conf,
            ds,
            len(dribbles),
            dr_move,
            dr_conf,
            ls,
            len(losses),
            lr_missing,
            lr_recovery,
            quality,
            round((perf_counter() - started) * 1000),
            breakdown,
            self._controlled_thresholds(),
            tuple(statistics),
            self._displacement_summary(statistics),
            self._displacement_histogram(statistics),
            tuple(dribble_stats),
            dribble_breakdown,
            self._dribble_thresholds(),
            evidence_gate,
        )
        warnings = [
            warning,
            "Technical-event analysis depends on ball-tracking quality.",
            "Image-space motion may include camera movement.",
        ]
        if dribbles:
            warnings.append("Dribble candidates do not indicate successful dribbles.")
        if losses:
            warnings.append("Ball-loss candidates do not prove possession was lost.")
        return self._result(
            tuple(controlled), tuple(dribbles), tuple(losses), diagnostics, tuple(warnings), None
        )

    def _evidence_gate(
        self,
        player_track_quality: float,
        ball_analysis_quality: float,
        interaction_analysis_quality: float,
        interaction_evidence_coverage_ratio: float,
    ) -> TechnicalEvidenceDiagnostics:
        thresholds = {
            "player_track_quality": self._settings.technical_event_min_player_track_quality,
            "ball_analysis_quality": self._settings.technical_event_min_ball_analysis_quality,
            "interaction_analysis_quality": self._settings.technical_event_min_interaction_quality,
            "interaction_evidence_coverage_ratio": self._settings.technical_event_min_evidence_coverage,
        }
        values = {
            "player_track_quality": player_track_quality,
            "ball_analysis_quality": ball_analysis_quality,
            "interaction_analysis_quality": interaction_analysis_quality,
            "interaction_evidence_coverage_ratio": interaction_evidence_coverage_ratio,
        }
        return TechnicalEvidenceDiagnostics(
            player_track_quality,
            ball_analysis_quality,
            interaction_analysis_quality,
            interaction_evidence_coverage_ratio,
            thresholds,
            tuple(name for name, value in values.items() if value < thresholds[name]),
        )

    def _controlled(
        self,
        interactions: InteractionAnalysisResult,
        p: dict[int, PlayerObservation],
        b: dict[int, BallObservation],
        quality: float,
    ) -> tuple[
        list[ControlledMovementCandidate],
        int,
        int,
        int,
        dict[str, int],
        list[dict[str, float | int | bool | str | None]],
    ]:
        accepted: list[ControlledMovementCandidate] = []
        short = low = 0
        statistics: list[dict[str, float | int | bool | str | None]] = []
        breakdown = {
            "duration": 0,
            "displacement": 0,
            "proximity": 0,
            "direction": 0,
            "coverage": 0,
            "confidence": 0,
        }
        for segment in interactions.segments:
            frames = [
                f for f in range(segment.start_frame, segment.end_frame + 1) if f in p and f in b
            ]
            if not frames:
                short += 1
                breakdown["coverage"] += 1
                self._log_rejection(
                    segment.segment_id,
                    segment.duration_seconds,
                    0.0,
                    0.0,
                    None,
                    0.0,
                    0.0,
                    "coverage",
                )
                statistics.append(
                    self._segment_statistics(
                        segment.segment_id,
                        segment.duration_seconds,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        None,
                        0.0,
                    )
                )
                statistics[-1]["accepted"] = False
                statistics[-1]["rejection_reason"] = "coverage"
                continue
            pp = [self._position(p[f]) for f in frames]
            bp = [b[f].center_point for f in frames]
            height = sum(p[f].bounding_box.y2 - p[f].bounding_box.y1 for f in frames) / len(frames)
            pd, bd = self._distance(pp[0], pp[-1]), self._distance(bp[0], bp[-1])
            norm = pd / height if height else 0.0
            proximity = sum(
                self._distance(pp[i], bp[i]) / height
                <= self._settings.interaction_proximity_threshold_ratio
                for i in range(len(frames))
            ) / len(frames)
            coverage = len(frames) / (segment.end_frame - segment.start_frame + 1)
            sim = self._cosine(
                (pp[-1][0] - pp[0][0], pp[-1][1] - pp[0][1]),
                (bp[-1][0] - bp[0][0], bp[-1][1] - bp[0][1]),
            )
            direction = (sim + 1) / 2 if sim is not None else 0.0
            confidence = self._clamp(
                0.25 * self._clamp(norm)
                + 0.25 * proximity
                + 0.20 * direction
                + 0.15 * coverage
                + 0.15 * quality
            )
            path_length = sum(
                self._distance(first, second) for first, second in zip(pp, pp[1:], strict=False)
            )
            statistic = self._segment_statistics(
                segment.segment_id,
                segment.duration_seconds,
                pd,
                height,
                norm,
                path_length,
                bd,
                proximity,
                sim,
                confidence,
            )
            statistics.append(statistic)
            self._log_segment(statistic)
            reason = self._controlled_rejection_reason(
                segment.duration_seconds, norm, proximity, sim, coverage, confidence
            )
            if reason is not None and reason != "confidence":
                statistic["accepted"] = False
                statistic["rejection_reason"] = reason
                short += 1
                breakdown[reason] += 1
                self._log_rejection(
                    segment.segment_id,
                    segment.duration_seconds,
                    norm,
                    proximity,
                    sim,
                    coverage,
                    confidence,
                    reason,
                )
            elif reason == "confidence":
                statistic["accepted"] = False
                statistic["rejection_reason"] = reason
                low += 1
                breakdown["confidence"] += 1
                self._log_rejection(
                    segment.segment_id,
                    segment.duration_seconds,
                    norm,
                    proximity,
                    sim,
                    coverage,
                    confidence,
                    reason,
                )
            else:
                statistic["accepted"] = True
                statistic["rejection_reason"] = None
                accepted.append(
                    ControlledMovementCandidate(
                        f"controlled-{segment.segment_id}",
                        segment.segment_id,
                        segment.start_frame,
                        segment.end_frame,
                        segment.start_time_seconds,
                        segment.end_time_seconds,
                        segment.duration_seconds,
                        pd,
                        norm,
                        bd,
                        proximity,
                        sim,
                        confidence,
                    )
                )
        return (
            accepted[: self._settings.technical_event_max_returned_events],
            len(interactions.segments),
            short,
            low,
            breakdown,
            statistics,
        )

    @staticmethod
    def _segment_statistics(
        segment_id: int,
        duration: float,
        displacement: float,
        height: float,
        normalized_displacement: float,
        path_length: float,
        ball_displacement: float,
        proximity: float,
        direction: float | None,
        confidence: float,
    ) -> dict[str, float | int | bool | str | None]:
        return {
            "segment_id": segment_id,
            "duration_seconds": duration,
            "player_displacement_pixels": displacement,
            "mean_player_height_pixels": height,
            "normalized_player_displacement": normalized_displacement,
            "player_path_length_pixels": path_length,
            "ball_displacement_pixels": ball_displacement,
            "proximity_frame_ratio": proximity,
            "direction_similarity": direction,
            "confidence": confidence,
        }

    @staticmethod
    def _log_segment(statistic: dict[str, float | int | bool | str | None]) -> None:
        _LOGGER.warning(
            "controlled_movement_segment_statistics segment_id=%s duration_seconds=%s "
            "player_displacement_pixels=%s mean_player_height_pixels=%s "
            "normalized_player_displacement=%s player_path_length_pixels=%s "
            "ball_displacement_pixels=%s proximity_frame_ratio=%s direction_similarity=%s confidence=%s",
            statistic["segment_id"],
            statistic["duration_seconds"],
            statistic["player_displacement_pixels"],
            statistic["mean_player_height_pixels"],
            statistic["normalized_player_displacement"],
            statistic["player_path_length_pixels"],
            statistic["ball_displacement_pixels"],
            statistic["proximity_frame_ratio"],
            statistic["direction_similarity"],
            statistic["confidence"],
        )

    @staticmethod
    def _displacement_summary(
        statistics: list[dict[str, float | int | bool | str | None]],
    ) -> dict[str, float]:
        values = sorted(float(item["normalized_player_displacement"] or 0.0) for item in statistics)
        if not values:
            return {
                "minimum": 0.0,
                "maximum": 0.0,
                "mean": 0.0,
                "median": 0.0,
                "p75": 0.0,
                "p90": 0.0,
            }
        return {
            "minimum": values[0],
            "maximum": values[-1],
            "mean": sum(values) / len(values),
            "median": TechnicalEventAnalyzer._percentile(values, 0.5),
            "p75": TechnicalEventAnalyzer._percentile(values, 0.75),
            "p90": TechnicalEventAnalyzer._percentile(values, 0.9),
        }

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        position = (len(values) - 1) * percentile
        lower, upper = int(position), min(int(position) + 1, len(values) - 1)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    @staticmethod
    def _displacement_histogram(
        statistics: list[dict[str, float | int | bool | str | None]],
    ) -> dict[str, int]:
        histogram = {"0.00-0.05": 0, "0.05-0.10": 0, "0.10-0.15": 0, "0.15-0.20": 0, "0.20+": 0}
        for statistic in statistics:
            value = float(statistic["normalized_player_displacement"] or 0.0)
            key = (
                "0.00-0.05"
                if value < 0.05
                else "0.05-0.10"
                if value < 0.10
                else "0.10-0.15"
                if value < 0.15
                else "0.15-0.20"
                if value < 0.20
                else "0.20+"
            )
            histogram[key] += 1
        return histogram

    def _controlled_rejection_reason(
        self,
        duration: float,
        displacement: float,
        proximity: float,
        direction: float | None,
        coverage: float,
        confidence: float,
    ) -> str | None:
        if duration < self._settings.controlled_min_duration_seconds:
            return "duration"
        if displacement < self._settings.controlled_min_player_displacement_ratio:
            return "displacement"
        if proximity < self._settings.controlled_min_ball_proximity_ratio:
            return "proximity"
        if coverage < self._settings.controlled_min_evidence_coverage:
            return "coverage"
        if direction is None or direction < self._settings.controlled_min_direction_similarity:
            return "direction"
        if confidence < self._settings.controlled_min_confidence:
            return "confidence"
        return None

    @staticmethod
    def _log_rejection(
        segment_id: int,
        duration: float,
        displacement: float,
        proximity: float,
        direction: float | None,
        coverage: float,
        confidence: float,
        reason: str,
    ) -> None:
        _LOGGER.warning(
            "controlled_movement_candidate_rejected segment_id=%s duration_seconds=%.3f "
            "normalized_player_displacement=%.3f proximity_frame_ratio=%.3f "
            "direction_similarity=%s evidence_coverage_ratio=%.3f confidence=%.3f "
            "rejection_reason=%s",
            segment_id,
            duration,
            displacement,
            proximity,
            direction,
            coverage,
            confidence,
            reason,
        )

    def _controlled_thresholds(self) -> dict[str, float]:
        return {
            "min_duration": self._settings.controlled_min_duration_seconds,
            "min_displacement": self._settings.controlled_min_player_displacement_ratio,
            "min_direction_similarity": self._settings.controlled_min_direction_similarity,
        }

    def _dribbles(
        self,
        controlled: list[ControlledMovementCandidate],
        p: dict[int, PlayerObservation],
        b: dict[int, BallObservation],
        movement: MovementResult | None,
        quality: float,
    ) -> tuple[
        list[DribbleCandidate],
        int,
        int,
        int,
        list[dict[str, float | int | bool | str | None]],
        dict[str, int],
    ]:
        result: list[DribbleCandidate] = []
        low_move = low_conf = 0
        statistics: list[dict[str, float | int | bool | str | None]] = []
        breakdown = {
            "duration": 0,
            "movement_evidence": 0,
            "proximity_persistence": 0,
            "direction_evidence": 0,
            "trajectory_quality": 0,
            "confidence": 0,
            "excessive_turn_frequency": 0,
        }
        weights = (
            self._settings.dribble_direct_displacement_weight
            + self._settings.dribble_path_length_weight
        )
        if abs(weights - 1.0) > 1e-9:
            raise TechnicalEventInputError("Dribble movement weights must sum to one.")
        for item in controlled:
            frames = [f for f in range(item.start_frame, item.end_frame + 1) if f in p and f in b]
            points = (
                [
                    (point.frame_index, point.position, point.bbox_height)
                    for point in movement.trajectory
                    if item.start_frame <= point.frame_index <= item.end_frame
                ]
                if movement
                else [
                    (f, self._position(p[f]), p[f].bounding_box.y2 - p[f].bounding_box.y1)
                    for f in frames
                ]
            )
            height = sum(point[2] for point in points) / len(points) if points else 0.0
            raw_path, filtered_path, ignored, rejected, raw_turns = self._filtered_path(
                points, height
            )
            direct = item.player_displacement_pixels / height if height else 0.0
            normalized_path = filtered_path / height if height else 0.0
            movement_component = self._settings.dribble_direct_displacement_weight * self._bounded(
                direct, self._settings.dribble_direct_displacement_scale
            ) + self._settings.dribble_path_length_weight * self._bounded(
                normalized_path, self._settings.dribble_path_length_scale
            )
            turns, ignored_small, ignored_adjacent = self._filter_turns(raw_turns)
            angles = [angle for _, angle in turns]
            changes = len(turns)
            turn_rate = changes / item.duration_seconds if item.duration_seconds else 0.0
            path_ratio = filtered_path / raw_path if raw_path else 0.0
            ball_coverage = len(frames) / max(item.end_frame - item.start_frame + 1, 1)
            player_coverage = len(points) / max(item.end_frame - item.start_frame + 1, 1)
            variability = self._turn_deviation(angles)
            trajectory_quality = self._clamp(
                0.30 * path_ratio
                + 0.20 * (1 - self._clamp(variability / 90))
                + 0.20 * ball_coverage
                + 0.20 * player_coverage
                + 0.10 * ball_coverage
            )
            persistence = len(frames) / max(item.end_frame - item.start_frame + 1, 1)
            direction_evidence = self._clamp(
                changes / max(self._settings.dribble_directional_min_direction_changes, 1)
            )
            straightness = self._clamp(
                item.player_displacement_pixels / filtered_path if filtered_path else 0.0
            )
            progressive = (
                item.duration_seconds >= self._settings.dribble_progressive_min_duration_seconds
                and direct >= self._settings.dribble_progressive_min_normalized_displacement
                and straightness >= self._settings.dribble_progressive_min_path_straightness
                and (item.direction_similarity or -1.0)
                >= self._settings.dribble_progressive_min_direction_similarity
            )
            directional = changes >= self._settings.dribble_directional_min_direction_changes
            subtype: (
                Literal["directional_dribble_candidate", "progressive_carry_candidate"] | None
            ) = (
                "directional_dribble_candidate"
                if directional
                else "progressive_carry_candidate"
                if progressive
                else None
            )
            direction_component = (
                direction_evidence
                if directional
                else self._clamp(((item.direction_similarity or -1.0) + 1) / 2)
            )
            conf = self._clamp(
                0.25 * item.confidence
                + 0.25 * movement_component
                + 0.20 * persistence
                + 0.10 * direction_component
                + 0.10 * trajectory_quality
                + 0.10 * quality
            )
            reasons: list[str] = []
            if item.duration_seconds < self._settings.dribble_min_duration_seconds:
                reasons.append("duration")
            if movement_component < self._settings.dribble_progressive_min_movement_component:
                reasons.append("movement_evidence")
            if persistence < self._settings.dribble_min_proximity_ratio:
                reasons.append("proximity_persistence")
            if subtype is None:
                reasons.append("direction_evidence")
            if directional and turn_rate > self._settings.dribble_max_direction_changes_per_second:
                reasons.append("excessive_turn_frequency")
            if trajectory_quality < self._settings.dribble_min_trajectory_quality:
                reasons.append("trajectory_quality")
            if conf < self._settings.dribble_min_confidence:
                reasons.append("confidence")
            statistic: dict[str, float | int | bool | str | None] = {
                "controlled_event_id": item.event_id,
                "source_interaction_segment_id": item.source_interaction_segment_id,
                "duration_seconds": item.duration_seconds,
                "player_displacement_pixels": item.player_displacement_pixels,
                "normalized_player_displacement": direct,
                "player_path_length_pixels": filtered_path,
                "normalized_player_path_length": normalized_path,
                "direct_displacement_to_path_ratio": item.player_displacement_pixels / filtered_path
                if filtered_path
                else 0.0,
                "direction_changes_inside_segment": changes,
                "raw_direction_changes": len(raw_turns),
                "filtered_direction_changes": changes,
                "raw_direction_changes_per_second": len(raw_turns) / item.duration_seconds,
                "filtered_direction_changes_per_second": turn_rate,
                "ignored_small_angle_turns": ignored_small,
                "ignored_adjacent_turns": ignored_adjacent,
                "mean_turn_angle_degrees": sum(angles) / len(angles) if angles else 0.0,
                "maximum_turn_angle_degrees": max(angles, default=0.0),
                "proximity_persistence": persistence,
                "mean_normalized_ball_distance": None,
                "ball_displacement_pixels": item.ball_displacement_pixels,
                "player_ball_direction_similarity": item.direction_similarity,
                "segment_movement_intensity": movement_component,
                "controlled_movement_confidence": item.confidence,
                "raw_dribble_confidence": conf,
                "raw_player_path_length_pixels": raw_path,
                "filtered_player_path_length_pixels": filtered_path,
                "ignored_tiny_movement_vectors": ignored,
                "rejected_path_jumps": rejected,
                "path_quality_ratio": trajectory_quality,
                "trajectory_quality_score": trajectory_quality,
                "accepted": not reasons,
                "rejection_reasons": ",".join(sorted(set(reasons))) if reasons else None,
            }
            statistics.append(statistic)
            for reason in reasons:
                breakdown[reason] += 1
            if reasons:
                low_move += int(any(reason != "confidence" for reason in reasons))
                low_conf += int(reasons == ["confidence"])
            else:
                if subtype is None:
                    raise InternalTechnicalEventDiagnosticsError(
                        "Accepted dribble candidates require a subtype."
                    )
                result.append(
                    DribbleCandidate(
                        f"dribble-{item.source_interaction_segment_id}",
                        item.event_id,
                        item.start_frame,
                        item.end_frame,
                        item.duration_seconds,
                        changes,
                        direct,
                        normalized_path,
                        movement_component,
                        subtype,
                        persistence,
                        straightness,
                        conf,
                        DRIBBLE_VERSION,
                    )
                )
        return (
            result[: self._settings.technical_event_max_returned_events],
            len(controlled),
            low_move,
            low_conf,
            statistics,
            breakdown,
        )

    def _filtered_path(
        self, points: list[tuple[int, tuple[float, float], float]], height: float
    ) -> tuple[float, float, int, int, list[tuple[int, float]]]:
        raw = filtered = 0.0
        ignored = rejected = 0
        angles: list[tuple[int, float]] = []
        vectors: list[tuple[int, tuple[float, float]]] = []
        for (first_frame, first, _), (second_frame, second, _) in zip(
            points, points[1:], strict=False
        ):
            vector = (second[0] - first[0], second[1] - first[1])
            distance = hypot(*vector)
            raw += distance
            if (
                second_frame != first_frame + 1
                or distance < self._settings.movement_minimum_vector_pixels
            ):
                ignored += 1
                continue
            if height and distance / height > self._settings.movement_max_normalized_jump:
                rejected += 1
                continue
            filtered += distance
            vectors.append((second_frame, vector))
        for (frame, first), (_, second) in zip(vectors, vectors[1:], strict=False):
            cosine = self._cosine(first, second)
            if cosine is not None:
                angles.append((frame, degrees(acos(cosine))))
        return raw, filtered, ignored, rejected, angles

    def _filter_turns(
        self, turns: list[tuple[int, float]]
    ) -> tuple[list[tuple[int, float]], int, int]:
        accepted: list[tuple[int, float]] = []
        small = adjacent = 0
        for frame, angle in turns:
            if angle < self._settings.dribble_minimum_direction_change_angle_degrees:
                small += 1
                continue
            if (
                accepted
                and frame - accepted[-1][0] < self._settings.dribble_minimum_turn_frame_separation
            ):
                adjacent += 1
                continue
            accepted.append((frame, angle))
        return accepted, small, adjacent

    @staticmethod
    def _turn_deviation(angles: list[float]) -> float:
        if len(angles) < 2:
            return 0.0
        mean = sum(angles) / len(angles)
        return float((sum((angle - mean) ** 2 for angle in angles) / len(angles)) ** 0.5)

    @staticmethod
    def _bounded(value: float, scale: float) -> float:
        return value / (value + scale) if value > 0 and scale > 0 else 0.0

    def _dribble_thresholds(self) -> dict[str, float]:
        return {
            "min_duration": self._settings.dribble_min_duration_seconds,
            "min_proximity_persistence": self._settings.dribble_min_proximity_ratio,
            "min_confidence": self._settings.dribble_min_confidence,
            "min_movement_component": self._settings.dribble_progressive_min_movement_component,
            "min_trajectory_quality": self._settings.dribble_min_trajectory_quality,
        }

    def _losses(
        self,
        interactions: InteractionAnalysisResult,
        p: dict[int, PlayerObservation],
        b: dict[int, BallObservation],
        quality: float,
        fps: float,
    ) -> tuple[list[BallLossCandidate], int, int, int]:
        result: list[BallLossCandidate] = []
        missing = recovery = 0
        window = round(self._settings.ball_loss_recovery_window_seconds * fps)
        for segment in interactions.segments:
            if segment.duration_seconds < self._settings.ball_loss_min_pre_interaction_seconds:
                continue
            frames = list(range(segment.end_frame + 1, segment.end_frame + window + 1))
            observed = [f for f in frames if f in p and f in b]
            if len(observed) < self._settings.ball_loss_min_post_evidence_frames:
                missing += 1
                continue
            ratios = [
                self._distance(self._position(p[f]), b[f].center_point)
                / (p[f].bounding_box.y2 - p[f].bounding_box.y1)
                for f in observed
            ]
            recovered = any(
                x <= self._settings.interaction_proximity_threshold_ratio for x in ratios
            )
            if recovered:
                recovery += 1
                continue
            away = (ratios[-1] - ratios[0]) / max(len(observed) - 1, 1)
            confidence = self._clamp(
                0.20 * segment.confidence
                + 0.20
                * self._clamp(
                    (max(ratios) - self._settings.ball_loss_min_separation_ratio)
                    / self._settings.ball_loss_min_separation_ratio
                )
                + 0.20 * self._clamp(away / self._settings.ball_loss_min_ball_away_speed_normalized)
                + 0.20 * (len(observed) / len(frames))
                + 0.20 * quality
            )
            if (
                max(ratios) >= self._settings.ball_loss_min_separation_ratio
                and away >= self._settings.ball_loss_min_ball_away_speed_normalized
                and confidence >= self._settings.ball_loss_min_confidence
            ):
                result.append(
                    BallLossCandidate(
                        f"ball-loss-{segment.segment_id}",
                        segment.segment_id,
                        observed[0],
                        p[observed[0]].timestamp_seconds,
                        segment.duration_seconds,
                        max(ratios),
                        len(observed),
                        False,
                        confidence,
                    )
                )
        return (
            result[: self._settings.technical_event_max_returned_events],
            len(interactions.segments),
            missing,
            recovery,
        )

    def _result(
        self,
        c: tuple[ControlledMovementCandidate, ...],
        d: tuple[DribbleCandidate, ...],
        losses: tuple[BallLossCandidate, ...],
        diag: TechnicalEventDiagnostics,
        warnings: tuple[str, ...],
        reason: str | None,
    ) -> TechnicalEventAnalysisResult:
        confidences = (
            [item.confidence for item in c]
            + [item.confidence for item in d]
            + [item.confidence for item in losses]
        )
        if any(not 0 <= confidence <= 1 for confidence in confidences):
            raise InternalTechnicalEventDiagnosticsError(
                "Technical-event confidence must be bounded."
            )
        return TechnicalEventAnalysisResult(
            c, d, losses, diag, tuple(dict.fromkeys(warnings)), reason
        )

    @staticmethod
    def _position(item: PlayerObservation) -> tuple[float, float]:
        box = item.bounding_box
        return ((box.x1 + box.x2) / 2, box.y2)

    @staticmethod
    def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
        return hypot(a[0] - b[0], a[1] - b[1])

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    def _cosine(self, a: tuple[float, float], b: tuple[float, float]) -> float | None:
        la, lb = hypot(*a), hypot(*b)
        return (
            None
            if min(la, lb) < self._settings.movement_minimum_vector_pixels
            else max(-1.0, min(1.0, (a[0] * b[0] + a[1] * b[1]) / (la * lb)))
        )

    def _direction_changes(self, points: list[tuple[float, float]]) -> int:
        changes = 0
        for first, second, third in zip(points, points[1:], points[2:], strict=False):
            similarity = self._cosine(
                (second[0] - first[0], second[1] - first[1]),
                (third[0] - second[0], third[1] - second[1]),
            )
            if similarity is not None and similarity < 0.866:
                changes += 1
        return changes
