"""Pure data contracts for provisional physical activity scoring."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PhysicalEvidenceDiagnostics:
    """Inputs captured at the physical-score evidence gate."""

    movement_quality: float | None
    visibility_ratio: float
    visible_duration_seconds: float | None
    movement_observations: int | None
    accepted_interval_ratio: float | None
    thresholds: dict[str, float | int]
    failed_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PhysicalScoreEvidence:
    movement_intensity: float
    active_time_ratio: float
    visibility_ratio: float
    continuity_ratio: float
    direction_component: float
    movement_analysis_quality: float
    movement_duration_seconds: float
    movement_observations: int
    accepted_interval_ratio: float


@dataclass(frozen=True, slots=True)
class PhysicalScoreResult:
    value: float | None
    level: int | None
    level_label: str | None
    level_midpoint: float | None
    confidence: float | None
    status: str
    version: str
    reason: str | None
    evidence: PhysicalScoreEvidence | None
    limitations: tuple[str, ...]
    explanation: str
    raw_score: float | None
    confidence_capped: bool
    processing_time_ms: int
    evidence_gate: PhysicalEvidenceDiagnostics | None = None
