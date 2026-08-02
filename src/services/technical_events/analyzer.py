"""O(n), deterministic candidate detection using existing tracking evidence only."""

from math import hypot
from time import perf_counter

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
)

CONTROLLED_VERSION = "controlled_movement_confidence_v0.1"
DRIBBLE_VERSION = "dribble_candidate_confidence_v0.1"
BALL_LOSS_VERSION = "ball_loss_candidate_confidence_v0.1"


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
        warning = (
            "Technical events are heuristic candidates and do not prove confirmed football actions."
        )
        if (
            player_track_quality < self._settings.technical_event_min_player_track_quality
            or ball_analysis_quality < self._settings.technical_event_min_ball_analysis_quality
            or interaction_analysis_quality < self._settings.technical_event_min_interaction_quality
            or interactions.interaction_evidence_coverage_ratio
            < self._settings.technical_event_min_evidence_coverage
        ):
            return self._result(
                (),
                (),
                (),
                TechnicalEventDiagnostics(
                    technical_event_analysis_quality=quality,
                    processing_time_ms=round((perf_counter() - started) * 1000),
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
        controlled, cs, cr_short, cr_conf = self._controlled(interactions, player, ball, quality)
        dribbles, ds, dr_move, dr_conf = self._dribbles(controlled, player, ball, movement, quality)
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

    def _controlled(
        self,
        interactions: InteractionAnalysisResult,
        p: dict[int, PlayerObservation],
        b: dict[int, BallObservation],
        quality: float,
    ) -> tuple[list[ControlledMovementCandidate], int, int, int]:
        accepted: list[ControlledMovementCandidate] = []
        short = low = 0
        for segment in interactions.segments:
            frames = [
                f for f in range(segment.start_frame, segment.end_frame + 1) if f in p and f in b
            ]
            if not frames:
                short += 1
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
            if (
                segment.duration_seconds < self._settings.controlled_min_duration_seconds
                or norm < self._settings.controlled_min_player_displacement_ratio
                or proximity < self._settings.controlled_min_ball_proximity_ratio
                or coverage < self._settings.controlled_min_evidence_coverage
                or sim is None
                or sim < self._settings.controlled_min_direction_similarity
            ):
                short += 1
            elif confidence < self._settings.controlled_min_confidence:
                low += 1
            else:
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
        )

    def _dribbles(
        self,
        controlled: list[ControlledMovementCandidate],
        p: dict[int, PlayerObservation],
        b: dict[int, BallObservation],
        movement: MovementResult | None,
        quality: float,
    ) -> tuple[list[DribbleCandidate], int, int, int]:
        result: list[DribbleCandidate] = []
        low_move = low_conf = 0
        for item in controlled:
            frames = [f for f in range(item.start_frame, item.end_frame + 1) if f in p and f in b]
            points = (
                [
                    point.position
                    for point in movement.trajectory
                    if item.start_frame <= point.frame_index <= item.end_frame
                ]
                if movement is not None
                else [self._position(p[f]) for f in frames]
            )
            changes = self._direction_changes(points)
            path = sum(self._distance(a, z) for a, z in zip(points, points[1:], strict=False))
            straight = item.player_displacement_pixels / path if path else 0.0
            persist = item.proximity_frame_ratio
            conf = self._clamp(
                0.30 * item.confidence
                + 0.20 * self._clamp(changes / max(self._settings.dribble_min_direction_changes, 1))
                + 0.20 * self._clamp(item.normalized_player_displacement)
                + 0.15 * persist
                + 0.15 * quality
            )
            if (
                item.duration_seconds < self._settings.dribble_min_duration_seconds
                or changes < self._settings.dribble_min_direction_changes
                or item.normalized_player_displacement
                < self._settings.dribble_min_normalized_displacement
                or persist < self._settings.dribble_min_proximity_ratio
            ):
                low_move += 1
            elif conf < self._settings.dribble_min_confidence:
                low_conf += 1
            else:
                result.append(
                    DribbleCandidate(
                        f"dribble-{item.source_interaction_segment_id}",
                        item.event_id,
                        item.start_frame,
                        item.end_frame,
                        item.duration_seconds,
                        changes,
                        item.normalized_player_displacement,
                        persist,
                        self._clamp(straight),
                        conf,
                    )
                )
        return (
            result[: self._settings.technical_event_max_returned_events],
            len(controlled),
            low_move,
            low_conf,
        )

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
