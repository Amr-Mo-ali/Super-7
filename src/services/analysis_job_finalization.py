"""Inactive fenced success-finalization port and strict frozen-snapshot validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from math import isfinite
from typing import Literal, Protocol
from uuid import UUID

from domain.analysis_job import _validate_datetime
from domain.analysis_job_attempt import AttemptAuthority, _validate_authority
from services.analysis_job_worker import (
    AnalysisJobWorkerPersistenceInvariantError,
    AnalysisJobWorkerPersistenceUnavailable,
    ClaimLost,
)

__all__ = [
    "AnalysisJobCompleted",
    "AnalysisJobCompletionAlreadyApplied",
    "AnalysisJobFinalizationPersistenceInvariantError",
    "AnalysisJobFinalizationPersistenceUnavailable",
    "AnalysisJobSuccessFinalizationRepository",
    "FinalizableAnalysisResult",
    "finalize_analysis_job_success",
]

AnalysisJobFinalizationPersistenceUnavailable = AnalysisJobWorkerPersistenceUnavailable
AnalysisJobFinalizationPersistenceInvariantError = AnalysisJobWorkerPersistenceInvariantError

_ANALYSIS_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_MODEL_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_VIDEO_HASH = re.compile(r"[0-9a-f]{64}")
_GIT_HASH = re.compile(r"[0-9a-f]{40}")
_RETAINED_METADATA = frozenset(
    {"video_hash", "configuration_profile", "pipeline_version", "git_commit"}
)
_RETAINED_DIAGNOSTIC_COUNTS = frozenset(
    {
        "frames_processed",
        "frames_with_player_detections",
        "total_person_detections",
        "tracks_created",
        "valid_candidate_tracks",
        "ball_detections",
    }
)
_RETAINED_DIAGNOSTIC_QUALITY = frozenset(
    {
        "ball_analysis_quality",
        "interaction_analysis_quality",
        "movement_analysis_quality",
        "technical_event_analysis_quality",
    }
)
_COMMON_RESULT = frozenset({"analysis_id", "status", "selected_player"})
_COMPLETED_RESULT = frozenset(
    {
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
        "analysis_metadata",
        "diagnostics",
    }
)
_AMBIGUOUS_RESULT = frozenset({"candidate_count"})
_NONCOMPLETED_RESULT = frozenset(
    {"candidate_count", "pipeline_state", "quality_gates", "diagnostics"}
)
_CALLBACK_FIELDS = frozenset(
    {
        "request_id",
        "video_id",
        "player_id",
        "status",
        "summary",
        "ratings",
        "overall",
        "detailed",
        "events",
        "error",
        "resultAvailability",
        "unavailabilityReason",
        "player",
        "overallConfidence",
    }
)


@dataclass(frozen=True, slots=True)
class FinalizableAnalysisResult:
    result_schema_version: Literal["analysis_result_v1"]
    result_json: bytes
    callback_schema_version: Literal["analysis_callback_snapshot_v1"]
    callback_json: bytes
    analysis_version: str
    model_version: str
    processing_duration_ms: int


@dataclass(frozen=True, slots=True)
class AnalysisJobCompleted:
    authority: AttemptAuthority
    finished_at: datetime
    callback_event_id: UUID

    def __post_init__(self) -> None:
        _validate_completion(self.authority, self.finished_at, self.callback_event_id)


@dataclass(frozen=True, slots=True)
class AnalysisJobCompletionAlreadyApplied:
    authority: AttemptAuthority
    finished_at: datetime
    callback_event_id: UUID

    def __post_init__(self) -> None:
        _validate_completion(self.authority, self.finished_at, self.callback_event_id)


class AnalysisJobSuccessFinalizationRepository(Protocol):
    async def finalize_success(
        self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
    ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost: ...


async def finalize_analysis_job_success(
    *,
    repository: AnalysisJobSuccessFinalizationRepository,
    authority: AttemptAuthority,
    result: FinalizableAnalysisResult,
) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
    _validate_success_input(authority, result)
    return await repository.finalize_success(authority=authority, result=result)


def _validate_completion(
    authority: AttemptAuthority, finished_at: datetime, event_id: UUID
) -> None:
    _validate_authority(authority)
    _validate_datetime(finished_at)
    if type(event_id) is not UUID:
        raise TypeError("Callback event ID must be an exact UUID.")
    if event_id.int == 0:
        raise ValueError("Callback event ID must not be nil.")


def _validate_success_input(
    authority: AttemptAuthority, result: FinalizableAnalysisResult
) -> tuple[dict[str, object], dict[str, object]]:
    _validate_authority(authority)
    if type(result) is not FinalizableAnalysisResult:
        raise TypeError("Result must be a FinalizableAnalysisResult.")
    if (
        type(result.result_schema_version) is not str
        or result.result_schema_version != "analysis_result_v1"
    ):
        raise ValueError("Unsupported result schema version.")
    if (
        type(result.callback_schema_version) is not str
        or result.callback_schema_version != "analysis_callback_snapshot_v1"
    ):
        raise ValueError("Unsupported callback snapshot schema version.")
    if type(result.analysis_version) is not str or not _ANALYSIS_VERSION.fullmatch(
        result.analysis_version
    ):
        raise ValueError("Analysis version must have bounded safe syntax.")
    if type(result.model_version) is not str or not _MODEL_VERSION.fullmatch(result.model_version):
        raise ValueError("Model version must have bounded safe syntax.")
    if (
        type(result.processing_duration_ms) is not int
        or not 0 <= result.processing_duration_ms <= 9_223_372_036_854_775_807
    ):
        raise ValueError("Processing duration must fit nonnegative BIGINT.")
    projected = _strict_json_object(result.result_json)
    callback = _strict_json_object(result.callback_json)
    status = projected.get("status")
    if type(status) is not str or projected.get("analysis_id") != str(authority.job_id):
        raise ValueError("Result identity or status is invalid.")
    expected = _COMMON_RESULT
    if status == "completed":
        expected |= _COMPLETED_RESULT
        if (
            projected.get("analysis_version") != result.analysis_version
            or projected.get("model_version") != result.model_version
        ):
            raise ValueError("Result version identities disagree.")
        versions = projected.get("algorithm_versions")
        if (
            not isinstance(versions, dict)
            or versions.get("player_detection") != result.model_version
        ):
            raise ValueError("Result model identities disagree.")
        duration = projected.get("processing_time_ms")
        if type(duration) is not int or duration < 0:
            raise ValueError("Pipeline processing time is invalid.")
    elif status == "ambiguous_target":
        expected |= _AMBIGUOUS_RESULT
    elif status in (
        "invalid_video",
        "no_players_detected",
        "no_valid_tracks",
        "failed",
        "player_detection_completed_tracking_not_available",
    ):
        expected |= _NONCOMPLETED_RESULT
    else:
        raise ValueError("Unsupported projected status.")
    if (
        projected.keys() != expected
        or projected.get("selected_player") is not None
        and status != "completed"
    ):
        raise ValueError("Result projection fields are invalid.")
    _validate_retained_result_fields(projected)
    if callback.keys() != _CALLBACK_FIELDS:
        raise ValueError("Callback snapshot fields are invalid.")
    if (
        callback.get("request_id") != str(authority.job_id)
        or type(callback.get("video_id")) is not str
        or type(callback.get("player_id")) is not str
        or callback.get("status") != ("COMPLETED" if status == "completed" else status)
        or callback.get("error") is not None
    ):
        raise ValueError("Callback snapshot identity or status is invalid.")
    return projected, callback


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
    if not isinstance(diagnostics, dict) or diagnostics.keys() != (
        _RETAINED_DIAGNOSTIC_COUNTS | _RETAINED_DIAGNOSTIC_QUALITY
    ):
        raise ValueError("Retained diagnostics shape is invalid.")
    if any(type(diagnostics[key]) is not int for key in _RETAINED_DIAGNOSTIC_COUNTS):
        raise ValueError("Retained diagnostic counts are invalid.")
    if any(
        diagnostics[key] is not None and type(diagnostics[key]) not in (int, Decimal, float)
        for key in _RETAINED_DIAGNOSTIC_QUALITY
    ):
        raise ValueError("Retained diagnostic quality is invalid.")
    if status != "completed":
        return
    metadata = projected["analysis_metadata"]
    if not isinstance(metadata, dict) or not metadata.keys() <= _RETAINED_METADATA:
        raise ValueError("Retained analysis metadata shape is invalid.")
    for key, value in metadata.items():
        if key == "video_hash":
            valid = type(value) is str and _VIDEO_HASH.fullmatch(value) is not None
        elif key == "git_commit":
            valid = value is None or type(value) is str and _GIT_HASH.fullmatch(value) is not None
        else:
            valid = type(value) is str and _ANALYSIS_VERSION.fullmatch(value) is not None
        if not valid:
            raise ValueError("Retained analysis metadata value is invalid.")
    versions = projected["algorithm_versions"]
    if not isinstance(versions, dict) or any(type(value) is not str for value in versions.values()):
        raise ValueError("Retained algorithm versions are invalid.")


def _strict_json_object(data: bytes) -> dict[str, object]:
    if type(data) is not bytes:
        raise TypeError("Snapshot must be frozen UTF-8 bytes.")
    try:
        decoded: object = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_pairs,
            parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Snapshot must be strict JSON.") from error
    if not isinstance(decoded, dict):
        raise ValueError("Snapshot must be a JSON object.")
    _validate_json_value(decoded)
    return decoded


def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"Unsupported JSON constant: {value}.")


def _validate_json_value(value: object) -> None:
    if value is None or type(value) in (bool,):
        return
    if type(value) is str:
        if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("JSON contains invalid Unicode text.")
        return
    if type(value) is int:
        if len(str(abs(value))) > 131_072:
            raise ValueError("JSON integer exceeds PostgreSQL numeric range.")
        return
    if type(value) is Decimal:
        if not value.is_finite() or not -16_383 <= value.adjusted() <= 131_071:
            raise ValueError("JSON number exceeds PostgreSQL numeric range.")
        return
    if type(value) is float:
        if not isfinite(value):
            raise ValueError("JSON number must be finite.")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_value(key)
            _validate_json_value(item)
        return
    raise ValueError("Snapshot contains unsupported JSON values.")
