"""O(n), deterministic analysis of possible ball-interaction segments."""

from collections.abc import Sequence
from math import isfinite
from time import perf_counter
from typing import Protocol

from core.config import Settings
from core.exceptions import (
    InteractionInputError,
    InteractionSegmentationError,
    InternalInteractionDiagnosticsError,
)
from services.interactions.confidence import CONFIDENCE_VERSION, confidence
from services.interactions.models import (
    BallObservation,
    FrameEvidence,
    InteractionAnalysisResult,
    InteractionDiagnostics,
    InteractionSegment,
    PlayerObservation,
)
from services.interactions.segment_builder import build_raw_segments


class BallInteractionAnalyzerProtocol(Protocol):
    def analyze(
        self,
        players: Sequence[PlayerObservation],
        balls: Sequence[BallObservation],
        fps: float,
        frame_dimensions: tuple[int, int],
        ball_analysis_quality: float,
        player_track_quality: float,
    ) -> InteractionAnalysisResult: ...


class BallInteractionAnalyzer:
    """Groups proximity evidence without asserting contact, control, or possession."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def analyze(
        self,
        players: Sequence[PlayerObservation],
        balls: Sequence[BallObservation],
        fps: float,
        frame_dimensions: tuple[int, int],
        ball_analysis_quality: float,
        player_track_quality: float,
    ) -> InteractionAnalysisResult:
        started = perf_counter()
        if fps <= 0 or frame_dimensions[0] <= 0 or frame_dimensions[1] <= 0:
            raise InteractionInputError("FPS and frame dimensions must be positive.")
        player_by_frame = self._index_players(players)
        ball_by_frame = self._index_balls(balls)
        evidence = self._evidence(player_by_frame, ball_by_frame)
        quality = max(0.0, min(1.0, (ball_analysis_quality + player_track_quality) / 2))
        warnings = [
            "Possible ball interaction is a proximity-based heuristic and does not prove ball contact or possession.",
            "Interaction analysis used image-space proximity only.",
        ]
        if any(item.state == "missing_evidence" for item in evidence):
            warnings.append("Ball visibility was insufficient for complete interaction analysis.")
        raw = build_raw_segments(evidence, self._settings)
        by_frame = {item.frame_index: item for item in evidence}
        bridged = sum(
            sum(by_frame[frame].state == "missing_evidence" for frame in run) for run in raw
        )
        if bridged:
            warnings.append("Short missing-ball gaps were bridged during interaction segmentation.")
        low_global_quality = (
            ball_analysis_quality < self._settings.interaction_min_ball_analysis_quality
            or player_track_quality < self._settings.interaction_min_player_track_quality
        )
        accepted: list[InteractionSegment] = []
        rejected_short = rejected_low_confidence = rejected_invalid = 0
        rejected_global = len(raw) if low_global_quality else 0
        if not low_global_quality:
            for run in raw:
                try:
                    segment = self._segment(
                        len(accepted) + 1,
                        run,
                        by_frame,
                        fps,
                        player_by_frame,
                        ball_by_frame,
                        player_track_quality,
                        ball_analysis_quality,
                    )
                except InteractionSegmentationError:
                    rejected_invalid += 1
                    continue
                if (
                    segment.candidate_frame_count < self._settings.interaction_min_segment_frames
                    or segment.duration_seconds
                    < self._settings.interaction_min_segment_duration_seconds
                ):
                    rejected_short += 1
                elif segment.confidence < self._settings.interaction_min_segment_confidence:
                    rejected_low_confidence += 1
                else:
                    accepted.append(segment)
        if low_global_quality:
            reason = "Ball and player tracking quality was insufficient for reliable interaction analysis."
            warnings.append(reason)
        else:
            reason = None
        accepted = accepted[: self._settings.interaction_max_returned_segments]
        observed = sum(item.state != "missing_evidence" for item in evidence)
        candidate_count = sum(item.state == "candidate" for item in evidence)
        total_span = len(evidence)
        diagnostics = InteractionDiagnostics(
            interaction_aligned_frames=sum(item.state != "missing_evidence" for item in evidence),
            interaction_candidate_frames=candidate_count,
            interaction_non_candidate_frames=sum(
                item.state == "non_candidate" for item in evidence
            ),
            interaction_missing_evidence_frames=sum(
                item.state == "missing_evidence" for item in evidence
            ),
            raw_interaction_segments=len(raw),
            accepted_interaction_segments=len(accepted),
            rejected_short_interaction_segments=rejected_short,
            rejected_low_confidence_interaction_segments=rejected_low_confidence,
            rejected_low_global_quality_interaction_segments=rejected_global,
            rejected_invalid_interaction_segments=rejected_invalid,
            bridged_interaction_gaps=bridged,
            maximum_bridged_gap_frames=self._settings.interaction_max_gap_frames,
            interaction_evidence_coverage_ratio=observed / total_span if total_span else 0.0,
            interaction_confidence_version=CONFIDENCE_VERSION,
            interaction_analysis_quality=quality,
            processing_time_ms=round((perf_counter() - started) * 1000),
        )
        result = InteractionAnalysisResult(
            segments=tuple(accepted),
            possible_ball_interaction_count=len(accepted),
            possible_ball_interaction_time_seconds=sum(item.duration_seconds for item in accepted),
            longest_possible_ball_interaction_seconds=max(
                (item.duration_seconds for item in accepted), default=0.0
            ),
            mean_possible_ball_interaction_confidence=(
                sum(item.confidence for item in accepted) / len(accepted) if accepted else None
            ),
            interaction_candidate_frames=candidate_count,
            interaction_observed_frames=observed,
            interaction_evidence_coverage_ratio=diagnostics.interaction_evidence_coverage_ratio,
            confidence_version=CONFIDENCE_VERSION,
            diagnostics=diagnostics,
            warnings=tuple(dict.fromkeys(warnings)),
            reason=reason,
        )
        self._validate(result)
        return result

    @staticmethod
    def _index_players(items: Sequence[PlayerObservation]) -> dict[int, PlayerObservation]:
        if len({item.frame_index for item in items}) != len(items):
            raise InteractionInputError("Duplicate selected-player frame indices are not allowed.")
        return {item.frame_index: item for item in sorted(items, key=lambda item: item.frame_index)}

    @staticmethod
    def _index_balls(items: Sequence[BallObservation]) -> dict[int, BallObservation]:
        if len({item.frame_index for item in items}) != len(items):
            raise InteractionInputError("Duplicate accepted-ball frame indices are not allowed.")
        return {item.frame_index: item for item in sorted(items, key=lambda item: item.frame_index)}

    def _evidence(
        self, players: dict[int, PlayerObservation], balls: dict[int, BallObservation]
    ) -> tuple[FrameEvidence, ...]:
        if not players and not balls:
            return ()
        start, end = min((*players, *balls)), max((*players, *balls))
        result: list[FrameEvidence] = []
        for frame in range(start, end + 1):
            player, ball = players.get(frame), balls.get(frame)
            if player is None or ball is None or not ball.accepted_by_ball_tracker:
                result.append(FrameEvidence(frame, "missing_evidence"))
                continue
            box, center = player.bounding_box, ball.center_point
            values = (box.x1, box.y1, box.x2, box.y2, *center, player.confidence, ball.confidence)
            height = box.y2 - box.y1
            if not all(isfinite(value) for value in values) or height <= 0:
                result.append(FrameEvidence(frame, "missing_evidence"))
                continue
            distance = (((box.x1 + box.x2) / 2 - center[0]) ** 2 + (box.y2 - center[1]) ** 2) ** 0.5
            normalized = distance / height
            state: str = (
                "candidate"
                if player.confidence >= self._settings.interaction_min_player_confidence
                and ball.confidence >= self._settings.interaction_min_ball_confidence
                and normalized <= self._settings.interaction_proximity_threshold_ratio
                else "non_candidate"
            )
            result.append(
                FrameEvidence(
                    frame,
                    "candidate" if state == "candidate" else "non_candidate",
                    distance,
                    normalized,
                    player.confidence,
                    ball.confidence,
                )
            )
        return tuple(result)

    def _segment(
        self,
        segment_id: int,
        run: tuple[int, ...],
        evidence: dict[int, FrameEvidence],
        fps: float,
        players: dict[int, PlayerObservation],
        balls: dict[int, BallObservation],
        player_quality: float,
        ball_quality: float,
    ) -> InteractionSegment:
        observed = [evidence[frame] for frame in run if evidence[frame].state != "missing_evidence"]
        candidates = [item for item in observed if item.state == "candidate"]
        if not candidates:
            raise InteractionSegmentationError(
                "A raw interaction segment must contain candidate evidence."
            )
        start, end = run[0], run[-1]
        duration = (end - start + 1) / fps
        coverage = len(observed) / (end - start + 1)
        distances = [item.distance_pixels for item in observed if item.distance_pixels is not None]
        normalized = [
            item.normalized_distance for item in observed if item.normalized_distance is not None
        ]
        player_conf = [
            item.player_confidence for item in observed if item.player_confidence is not None
        ]
        ball_conf = [item.ball_confidence for item in observed if item.ball_confidence is not None]
        mean_normalized = sum(normalized) / len(normalized)
        detection = (sum(player_conf) / len(player_conf) + sum(ball_conf) / len(ball_conf)) / 2
        return InteractionSegment(
            segment_id,
            start,
            end,
            players[start].timestamp_seconds,
            balls[end].timestamp_seconds,
            duration,
            len(observed),
            len(run) - len(observed),
            len(candidates),
            coverage,
            sum(distances) / len(distances),
            min(distances),
            mean_normalized,
            min(normalized),
            sum(player_conf) / len(player_conf),
            sum(ball_conf) / len(ball_conf),
            confidence(
                self._settings,
                mean_normalized,
                coverage,
                duration,
                detection,
                player_quality,
                ball_quality,
            ),
        )

    @staticmethod
    def _validate(result: InteractionAnalysisResult) -> None:
        d = result.diagnostics
        if d.raw_interaction_segments != (
            d.accepted_interaction_segments
            + d.rejected_short_interaction_segments
            + d.rejected_low_confidence_interaction_segments
            + d.rejected_low_global_quality_interaction_segments
            + d.rejected_invalid_interaction_segments
        ):
            raise InternalInteractionDiagnosticsError(
                "Interaction segment accounting must reconcile."
            )
        previous_end = -1
        for segment in result.segments:
            if segment.start_frame > segment.end_frame or segment.start_frame <= previous_end:
                raise InternalInteractionDiagnosticsError(
                    "Interaction segments must be sorted and non-overlapping."
                )
            if not 0 <= segment.evidence_coverage_ratio <= 1 or not 0 <= segment.confidence <= 1:
                raise InternalInteractionDiagnosticsError(
                    "Interaction coverage and confidence must be bounded."
                )
            if (
                segment.observed_frame_count + segment.bridged_gap_frames
                != segment.end_frame - segment.start_frame + 1
            ):
                raise InternalInteractionDiagnosticsError(
                    "Observed frames must exclude bridged gaps."
                )
            previous_end = segment.end_frame
