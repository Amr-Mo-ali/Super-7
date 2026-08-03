"""Extension point for future tracklet stitching; intentionally no-op in v0.1."""

from typing import Protocol

from services.selection import PlayerTrack


class TrackletStitcherProtocol(Protocol):
    def stitch(self, tracks: tuple[PlayerTrack, ...]) -> tuple[PlayerTrack, ...]: ...


class NoOpTrackletStitcher:
    """Preserves tracker output without appearance inference or ReID."""

    def stitch(self, tracks: tuple[PlayerTrack, ...]) -> tuple[PlayerTrack, ...]:
        return tracks
