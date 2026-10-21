"""Pure Slice 4 conversion of one child success into frozen result/callback snapshots."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from pydantic import TypeAdapter

from adapters.analysis_callback_presentation import callback_payload
from domain.analysis_job_attempt import WorkerClaim
from schemas.analysis import AmbiguousResponse, AnalyzeResponse, CompletedResponse
from services._analysis_result_v1_fields import (
    _AMBIGUOUS_FIELDS,
    _COMMON_FIELDS,
    _COMPLETED_FIELDS,
    _DIAGNOSTIC_FIELDS,
    _GIT_HASH,
    _METADATA_FIELDS,
    _NONCOMPLETED_FIELDS,
    _TOKEN,
    _VIDEO_HASH,
)
from services.analysis_job_finalization import (
    FinalizableAnalysisResult,
    _strict_json_object,
    _validate_success_input,
)
from services.process_contracts import ChildAnalysisSuccess

_RESPONSE_ADAPTER: TypeAdapter[AnalyzeResponse] = TypeAdapter(AnalyzeResponse)


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
    selected = {key: materialized[key] for key in _COMMON_FIELDS}
    if isinstance(response, CompletedResponse):
        if (
            response.analysis_version != child.analysis_version
            or response.model_version != child.model_version
        ):
            raise ValueError("Child and completed-result versions disagree.")
        selected.update({key: materialized[key] for key in _COMPLETED_FIELDS})
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
        selected.update({key: materialized[key] for key in _AMBIGUOUS_FIELDS})
    else:
        selected.update({key: materialized[key] for key in _NONCOMPLETED_FIELDS})
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
    selected = {key: metadata[key] for key in _METADATA_FIELDS if key in metadata}
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
    return {key: value[key] for key in _DIAGNOSTIC_FIELDS}


def _freeze(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode(
        "utf-8"
    )
