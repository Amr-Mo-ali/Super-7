"""Stateful ByteTrack adapter isolated from detector and API code."""

from collections.abc import Sequence
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol, cast

import numpy as np

from core.config import Settings
from services.player_detector import Detection


@dataclass(frozen=True, slots=True)
class Track:
    track_id: int
    frame_index: int
    confidence: float
    bounding_box: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class ByteTrackDetections:
    """Validated Results-like container required by Ultralytics ByteTrack."""

    xyxy: np.ndarray
    conf: np.ndarray
    cls: np.ndarray

    def __post_init__(self) -> None:
        if self.xyxy.ndim != 2 or self.xyxy.shape[1:] != (4,):
            raise ValueError("xyxy must have shape (N, 4).")
        if len(self.xyxy) != len(self.conf) or len(self.conf) != len(self.cls):
            raise ValueError("Detection arrays must have equal lengths.")
        if not np.isfinite(self.xyxy).all() or not np.isfinite(self.conf).all():
            raise ValueError("Detection arrays must be finite.")
        if np.any(self.xyxy[:, 2] <= self.xyxy[:, 0]) or np.any(self.xyxy[:, 3] <= self.xyxy[:, 1]):
            raise ValueError("Detection boxes must have positive dimensions.")

    @property
    def xywh(self) -> np.ndarray:
        boxes = self.xyxy.copy()
        boxes[:, 0] = (self.xyxy[:, 0] + self.xyxy[:, 2]) / 2
        boxes[:, 1] = (self.xyxy[:, 1] + self.xyxy[:, 3]) / 2
        boxes[:, 2] = self.xyxy[:, 2] - self.xyxy[:, 0]
        boxes[:, 3] = self.xyxy[:, 3] - self.xyxy[:, 1]
        return boxes

    def __len__(self) -> int:
        return len(self.xyxy)

    def __getitem__(self, index: object) -> "ByteTrackDetections":
        array_index = cast(Any, index)
        return ByteTrackDetections(
            np.atleast_2d(self.xyxy[array_index]).astype(np.float32),
            np.atleast_1d(self.conf[array_index]).astype(np.float32),
            np.atleast_1d(self.cls[array_index]).astype(np.float32),
        )


class TrackerProtocol(Protocol):
    @property
    def tracks_created(self) -> int: ...

    @property
    def lost_tracks(self) -> int: ...

    @property
    def track_switches(self) -> int: ...

    def update(self, detections: Sequence[Detection]) -> Sequence[Track]: ...


class ByteTrackTracker:
    """Ultralytics ByteTrack state maintained for one video analysis."""

    def __init__(self, settings: Settings) -> None:
        self._args = SimpleNamespace(
            track_high_thresh=settings.tracker_high_threshold,
            track_low_thresh=settings.tracker_low_threshold,
            new_track_thresh=settings.tracker_high_threshold,
            track_buffer=settings.tracker_buffer,
            match_thresh=settings.tracker_match_threshold,
            fuse_score=True,
        )
        self._tracker: Any | None = None
        self.tracks_created = 0
        self.lost_tracks = 0
        self.track_switches = 0
        self._seen: set[int] = set()

    def update(self, detections: Sequence[Detection]) -> Sequence[Track]:
        """Convert domain detections to ByteTrack input and normalize associations."""
        payload = self._payload(detections)
        rows = self._get_tracker().update(payload)
        tracks = tuple(
            Track(
                int(row[4]),
                detections[int(row[7])].frame_index,
                float(row[5]),
                (float(row[0]), float(row[1]), float(row[2]), float(row[3])),
            )
            for row in rows
            if len(row) >= 8 and 0 <= int(row[7]) < len(detections)
        )
        self._seen.update(track.track_id for track in tracks)
        self.tracks_created = len(self._seen)
        return tracks

    def _get_tracker(self) -> Any:
        """Create ByteTrack only on first inference, after application import succeeds."""
        if self._tracker is None:
            from ultralytics.trackers.byte_tracker import BYTETracker

            self._tracker = BYTETracker(self._args)  # type: ignore[no-untyped-call]
        return self._tracker

    @staticmethod
    def _payload(detections: Sequence[Detection]) -> ByteTrackDetections:
        """Convert domain detections without exposing Ultralytics types."""
        xyxy = np.asarray(
            [
                [d.bounding_box.x1, d.bounding_box.y1, d.bounding_box.x2, d.bounding_box.y2]
                for d in detections
            ],
            dtype=np.float32,
        ).reshape((-1, 4))
        return ByteTrackDetections(
            xyxy,
            np.asarray([d.confidence for d in detections], dtype=np.float32),
            np.zeros(len(detections), dtype=np.float32),
        )
