"""Linear segment construction that only bridges missing evidence."""

from collections.abc import Sequence

from core.config import Settings
from services.interactions.models import FrameEvidence


def build_raw_segments(
    evidence: Sequence[FrameEvidence], settings: Settings
) -> tuple[tuple[int, ...], ...]:
    """Return candidate runs, permitting only short missing-evidence gaps."""
    segments: list[tuple[int, ...]] = []
    current: list[int] = []
    missing: list[int] = []
    for item in evidence:
        if item.state == "candidate":
            if not current:
                current = [item.frame_index]
            elif missing and len(missing) <= settings.interaction_max_gap_frames:
                current.extend(missing)
                current.append(item.frame_index)
            elif missing:
                segments.append(tuple(current))
                current = [item.frame_index]
            else:
                current.append(item.frame_index)
            missing = []
        elif item.state == "missing_evidence" and current:
            missing.append(item.frame_index)
        else:
            if current:
                segments.append(tuple(current))
            current, missing = [], []
    if current:
        segments.append(tuple(current))
    return tuple(segments)
