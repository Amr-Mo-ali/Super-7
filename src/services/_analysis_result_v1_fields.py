"""Ordered projection fields and strict retained analysis_result_v1 field policy."""

from __future__ import annotations

import re
from decimal import Decimal

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_VIDEO_HASH = re.compile(r"[0-9a-f]{64}")
_GIT_HASH = re.compile(r"[0-9a-f]{40}")

_COMMON_FIELDS = ("analysis_id", "status", "selected_player")
_COMPLETED_FIELDS = (
    "video",
    "tracking",
    "features",
    "interaction_analysis",
    "technical_event_analysis",
    "pass_detection",
    "shot_detection",
    "scores",
    "player_rating_summary",
    "result_availability",
    "unavailability_reason",
    "analysis_version",
    "model_version",
    "processing_time_ms",
    "pipeline_state",
    "algorithm_versions",
    "quality_gates",
)
_COMPLETED_RESULT_FIELDS = (*_COMPLETED_FIELDS, "analysis_metadata", "diagnostics")
_AMBIGUOUS_FIELDS = ("candidate_count",)
_NONCOMPLETED_FIELDS = ("candidate_count", "pipeline_state", "quality_gates")
_NONCOMPLETED_RESULT_FIELDS = (*_NONCOMPLETED_FIELDS, "diagnostics")
_DIAGNOSTIC_COUNT_FIELDS = (
    "frames_processed",
    "frames_with_player_detections",
    "total_person_detections",
    "tracks_created",
    "valid_candidate_tracks",
    "ball_detections",
)
_DIAGNOSTIC_QUALITY_FIELDS = (
    "ball_analysis_quality",
    "interaction_analysis_quality",
    "movement_analysis_quality",
    "technical_event_analysis_quality",
)
_DIAGNOSTIC_FIELDS = _DIAGNOSTIC_COUNT_FIELDS + _DIAGNOSTIC_QUALITY_FIELDS
_METADATA_FIELDS = ("video_hash", "configuration_profile", "pipeline_version", "git_commit")


def _result_fields(status: object) -> frozenset[str] | None:
    variant: tuple[str, ...]
    if status == "completed":
        variant = _COMPLETED_RESULT_FIELDS
    elif status == "ambiguous_target":
        variant = _AMBIGUOUS_FIELDS
    elif status in (
        "invalid_video",
        "no_players_detected",
        "no_valid_tracks",
        "failed",
        "player_detection_completed_tracking_not_available",
    ):
        variant = _NONCOMPLETED_RESULT_FIELDS
    else:
        return None
    return frozenset((*_COMMON_FIELDS, *variant))


def _validate_retained_result_fields(projected: dict[str, object]) -> None:
    status = projected["status"]
    if status != "completed" and type(projected["candidate_count"]) is not int:
        raise ValueError("Retained candidate count is invalid.")
    if status == "ambiguous_target":
        return
    if type(projected["pipeline_state"]) is not str or not isinstance(
        projected["quality_gates"], dict
    ):
        raise ValueError("Retained pipeline state or quality gates are invalid.")
    if status == "completed" and projected["pipeline_state"] != "COMPLETE":
        raise ValueError("Retained completed pipeline state is invalid.")
    diagnostics = projected["diagnostics"]
    count_fields = frozenset(_DIAGNOSTIC_COUNT_FIELDS)
    quality_fields = frozenset(_DIAGNOSTIC_QUALITY_FIELDS)
    if not isinstance(diagnostics, dict) or diagnostics.keys() != count_fields | quality_fields:
        raise ValueError("Retained diagnostics shape is invalid.")
    if any(type(diagnostics[key]) is not int for key in count_fields):
        raise ValueError("Retained diagnostic counts are invalid.")
    if any(
        diagnostics[key] is not None and type(diagnostics[key]) not in (int, Decimal, float)
        for key in quality_fields
    ):
        raise ValueError("Retained diagnostic quality is invalid.")
    if status != "completed":
        return
    metadata = projected["analysis_metadata"]
    if not isinstance(metadata, dict) or not metadata.keys() <= frozenset(_METADATA_FIELDS):
        raise ValueError("Retained analysis metadata shape is invalid.")
    for key, value in metadata.items():
        if key == "video_hash":
            valid = type(value) is str and _VIDEO_HASH.fullmatch(value) is not None
        elif key == "git_commit":
            valid = value is None or type(value) is str and _GIT_HASH.fullmatch(value) is not None
        else:
            valid = type(value) is str and _TOKEN.fullmatch(value) is not None
        if not valid:
            raise ValueError("Retained analysis metadata value is invalid.")
    versions = projected["algorithm_versions"]
    if not isinstance(versions, dict) or any(type(value) is not str for value in versions.values()):
        raise ValueError("Retained algorithm versions are invalid.")
