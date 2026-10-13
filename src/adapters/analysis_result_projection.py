"""Pure Slice 4 conversion of one child success into frozen result/callback snapshots."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Final

from pydantic import TypeAdapter

from adapters.analysis_callback_presentation import callback_payload
from domain.analysis_job_attempt import WorkerClaim
from schemas.analysis import AmbiguousResponse, AnalyzeResponse, CompletedResponse
from services.analysis_job_finalization import (
    FinalizableAnalysisResult,
    _strict_json_object,
    _validate_success_input,
)
from services.process_contracts import ChildAnalysisSuccess

_RESPONSE_ADAPTER: TypeAdapter[AnalyzeResponse] = TypeAdapter(AnalyzeResponse)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_GIT_HASH = re.compile(r"[0-9a-f]{40}")
_VIDEO_HASH = re.compile(r"[0-9a-f]{64}")
_COMMON: Final = ("analysis_id", "status", "selected_player")
_COMPLETED: Final = (
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
_DIAGNOSTICS: Final = (
    "frames_processed",
    "frames_with_player_detections",
    "total_person_detections",
    "tracks_created",
    "valid_candidate_tracks",
    "ball_detections",
    "ball_analysis_quality",
    "interaction_analysis_quality",
    "movement_analysis_quality",
    "technical_event_analysis_quality",
)
_METADATA: Final = ("video_hash", "configuration_profile", "pipeline_version", "git_commit")


def convert_child_success(
    *,
    child: ChildAnalysisSuccess,
    response: AnalyzeResponse,
    claim: WorkerClaim,
    model_catalog: Mapping[str, str],
) -> FinalizableAnalysisResult:
    """Validate original bytes before coercion; freeze both projections before I/O."""
    if not isinstance(child, ChildAnalysisSuccess) or not isinstance(claim, WorkerClaim):
        raise TypeError("A child success and WorkerClaim are required.")
    _strict_json_object(child.response_json.encode("utf-8"))
    validated: AnalyzeResponse = _RESPONSE_ADAPTER.validate_json(child.response_json)
    if validated != response:
        raise ValueError("Child response differs from the validated response.")
    if (
        child.analysis_id != str(claim.authority.job_id)
        or response.analysis_id != child.analysis_id
        or claim.job.job_id != claim.authority.job_id
        or child.analysis_version != claim.job.analysis_version
    ):
        raise ValueError("Child and claim identities disagree.")
    if child.model_version not in model_catalog:
        raise ValueError("Child model pair has no configured safe identity.")
    logical_model = model_catalog[child.model_version]
    if type(logical_model) is not str or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", logical_model
    ):
        raise ValueError("Configured model identity is invalid.")
    materialized = response.model_dump(mode="json")
    selected = {key: materialized[key] for key in _COMMON}
    if isinstance(response, CompletedResponse):
        if (
            response.analysis_version != child.analysis_version
            or response.model_version != child.model_version
        ):
            raise ValueError("Child and completed-result versions disagree.")
        selected.update({key: materialized[key] for key in _COMPLETED})
        selected["model_version"] = logical_model
        versions = materialized["algorithm_versions"]
        if not isinstance(versions, dict):
            raise ValueError("Algorithm versions must be an object.")
        versions["player_detection"] = logical_model
        metadata = materialized["analysis_metadata"]
        if not isinstance(metadata, dict):
            raise ValueError("Analysis metadata must be an object.")
        selected["analysis_metadata"] = _metadata_projection(metadata)
        selected["diagnostics"] = _diagnostics_projection(materialized["diagnostics"])
    elif isinstance(response, AmbiguousResponse):
        selected["candidate_count"] = materialized["candidate_count"]
    else:
        for key in ("candidate_count", "pipeline_state", "quality_gates"):
            selected[key] = materialized[key]
        selected["diagnostics"] = _diagnostics_projection(materialized["diagnostics"])
    result_json = _freeze(selected)
    reconstructed = dict(_strict_json_object(result_json))
    reconstructed["warnings"] = []
    reconstructed["metadata"] = {}
    projected_response: AnalyzeResponse = _RESPONSE_ADAPTER.validate_python(reconstructed)
    payload = callback_payload(claim.job.video_id, claim.job.player_id, projected_response)
    callback_json = _freeze(payload.model_dump(mode="json", by_alias=True))
    result = FinalizableAnalysisResult(
        "analysis_result_v1",
        result_json,
        "analysis_callback_snapshot_v1",
        callback_json,
        child.analysis_version,
        logical_model,
        child.processing_duration_ms,
    )
    _validate_success_input(claim.authority, result)
    return result


def _metadata_projection(metadata: dict[str, object]) -> dict[str, object]:
    selected = {key: metadata[key] for key in _METADATA if key in metadata}
    for key, value in selected.items():
        if key == "video_hash":
            if type(value) is not str or not _VIDEO_HASH.fullmatch(value):
                raise ValueError("Video hash metadata is invalid.")
        elif key == "git_commit":
            if value is not None and (type(value) is not str or not _GIT_HASH.fullmatch(value)):
                raise ValueError("Git commit metadata is invalid.")
        elif type(value) is not str or not _TOKEN.fullmatch(value):
            raise ValueError("Retained analysis metadata token is invalid.")
    return selected


def _diagnostics_projection(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Diagnostics must be an object.")
    return {key: value[key] for key in _DIAGNOSTICS}


def _freeze(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )
