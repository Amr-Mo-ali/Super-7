"""Bounded, best-effort logging for coarse analysis-stage durations."""

import logging
from typing import Literal

type _AnalysisStage = Literal[
    "input_materialization",
    "validation",
    "tracking_total",
    "reproducibility_hashing",
    "target_segment_resolution",
    "post_processing_scoring",
    "debug_work",
    "child_serialization",
    "child_cleanup",
    "process_ipc_parent_validation",
    "parent_response_mapping",
]
type _StageRole = Literal["child", "parent"]
type _StageOutcome = Literal["success", "unavailable", "failed", "cancelled", "skipped"]


def _log_analysis_stage_timing(
    logger: logging.Logger,
    *,
    analysis_id: str,
    stage: _AnalysisStage,
    role: _StageRole,
    outcome: _StageOutcome,
    duration_ms: int,
) -> None:
    """Emit one fixed-shape record without allowing logging failure to escape."""
    try:
        logger.info(
            "analysis_stage_timing analysis_id=%s timing_schema_version=1 stage=%s "
            "role=%s outcome=%s duration_ms=%s",
            analysis_id,
            stage,
            role,
            outcome,
            max(0, duration_ms),
        )
    except Exception:
        pass
