"""Framework-neutral Event Arbitration V0.1 contracts."""

from dataclasses import dataclass
from typing import Literal

from services.event_arbitration.config import VERSION

EventType = Literal["controlled_movement", "dribble", "ball_loss", "pass", "shot"]
ArbitrationStatus = Literal["accepted", "suppressed_duplicate", "ambiguous", "unresolved_conflict"]


@dataclass(frozen=True, slots=True)
class EventCandidateRef:
    event_id: str
    event_type: EventType
    start_frame: int
    release_frame: int | None
    end_frame: int
    possessor_track_id: int | None
    receiver_track_id: int | None
    confidence: float
    trajectory_quality: float | None
    distance_pixels: float | None
    source_version: str
    preparation_confidence: float | None = None
    release_confidence: float | None = None
    follow_through_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class EventConflict:
    conflict_id: str
    candidate_ids: tuple[str, ...]
    conflict_type: str
    temporal_overlap_ratio: float
    same_release_frame: bool
    same_possessor: bool
    same_trajectory_signature: bool
    decision: ArbitrationStatus
    explanation: str
    limitations: tuple[str, ...]
    version: str = VERSION


@dataclass(frozen=True, slots=True)
class ArbitratedEvent:
    public_event_id: str
    event_type: EventType | None
    status: ArbitrationStatus
    event_confidence: float
    arbitration_confidence: float
    start_frame: int
    release_frame: int | None
    end_frame: int
    source_candidate_ids: tuple[str, ...]
    suppressed_candidate_ids: tuple[str, ...]
    explanation: str
    limitations: tuple[str, ...]
    candidate_types: tuple[EventType, ...]
    version: str = VERSION


@dataclass(frozen=True, slots=True)
class ArbitrationResult:
    events: tuple[ArbitratedEvent, ...]
    conflicts: tuple[EventConflict, ...]
    raw_candidate_count: int
    public_event_count: int
    suppressed_duplicate_count: int
    ambiguous_conflict_count: int
    version: str = VERSION
