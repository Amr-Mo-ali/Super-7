"""Pure target-player selection rules."""

from dataclasses import dataclass
from typing import Protocol

from core.config import Settings


@dataclass(frozen=True, slots=True)
class PlayerTrack:
    track_id: int
    visible_frames: int
    total_frames: int
    longest_segment: int
    lost_track_count: int
    average_confidence: float
    ball_proximity_frames: int
    ball_tracking_available: bool

    @property
    def visibility_ratio(self) -> float:
        return self.visible_frames / self.total_frames if self.total_frames else 0.0

    @property
    def ball_proximity_ratio(self) -> float:
        return self.ball_proximity_frames / self.visible_frames if self.visible_frames else 0.0


@dataclass(frozen=True, slots=True)
class Selection:
    track: PlayerTrack
    method: str
    score: float
    visibility_contribution: float
    ball_contribution: float
    segment_id: int | None = None
    segment_start_frame: int | None = None
    segment_end_frame: int | None = None
    segment_duration_seconds: float | None = None


class TargetPlayerSelector(Protocol):
    def rank(self, tracks: tuple[PlayerTrack, ...]) -> tuple[Selection, ...]: ...
    def select(self, tracks: tuple[PlayerTrack, ...]) -> Selection | None: ...


class WeightedTargetPlayerSelector:
    """Selects one qualified track or returns null for ambiguous evidence."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def select(self, tracks: tuple[PlayerTrack, ...]) -> Selection | None:
        ordered = self.rank(tracks)
        if not ordered:
            return None
        if (
            len(ordered) > 1
            and ordered[0].score - ordered[1].score < self._settings.selection_margin
        ):
            return None
        return ordered[0]

    def rank(self, tracks: tuple[PlayerTrack, ...]) -> tuple[Selection, ...]:
        """Return all qualified candidates ordered by configured score."""
        return tuple(
            sorted(
                (self._score(track) for track in tracks if self._qualified(track)),
                key=lambda item: item.score,
                reverse=True,
            )
        )

    def _qualified(self, track: PlayerTrack) -> bool:
        return (
            track.visibility_ratio >= self._settings.minimum_visibility_ratio
            and track.longest_segment >= self._settings.minimum_continuous_track_length
            and track.average_confidence >= self._settings.minimum_detection_confidence
        )

    def _score(self, track: PlayerTrack) -> Selection:
        visibility = track.visibility_ratio
        return Selection(track, "visibility_and_track_continuity", visibility, visibility, 0.0)
