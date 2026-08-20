"""Pickle-safe child/parent analysis contracts with no runtime dependencies."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from pydantic import TypeAdapter

from schemas.analysis import AnalyzeResponse

CHILD_ANALYSIS_SCHEMA_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_RESPONSE_ADAPTER: TypeAdapter[AnalyzeResponse] = TypeAdapter(AnalyzeResponse)


@dataclass(frozen=True, slots=True)
class ChildAnalysisRequest:
    analysis_id: str
    video_id: str
    player_id: str
    video_reference: str
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for value in (self.analysis_id, self.video_id, self.player_id):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError("child analysis identifiers must be safe")
        _validate_reference(self.video_reference)
        _validate_version(self.schema_version)


@dataclass(frozen=True, slots=True)
class ChildAnalysisSuccess:
    analysis_id: str
    response_json: str
    analysis_version: str
    model_version: str
    processing_duration_ms: int
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_result(self.analysis_id, self.processing_duration_ms, self.schema_version)
        if not self.analysis_version or _has_control(self.analysis_version):
            raise ValueError("analysis_version must be a safe non-empty value")
        if not self.model_version or _has_control(self.model_version):
            raise ValueError("model_version must be a safe non-empty value")
        try:
            decoded = json.loads(self.response_json)
        except json.JSONDecodeError as error:
            raise ValueError("response_json must be valid JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("response_json must be a JSON object")


@dataclass(frozen=True, slots=True)
class ChildAnalysisFailure:
    analysis_id: str
    error_code: str
    public_message: str
    processing_duration_ms: int
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_result(self.analysis_id, self.processing_duration_ms, self.schema_version)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", self.error_code):
            raise ValueError("child failure code must be a safe exception class name")
        if self.public_message != "Analysis could not be completed.":
            raise ValueError("child failures must use the fixed sanitized message")


@dataclass(frozen=True, slots=True)
class ChildAnalysisCancelled:
    analysis_id: str
    processing_duration_ms: int
    schema_version: int = CHILD_ANALYSIS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _validate_result(self.analysis_id, self.processing_duration_ms, self.schema_version)


type ChildAnalysisResult = ChildAnalysisSuccess | ChildAnalysisFailure | ChildAnalysisCancelled


@dataclass(frozen=True, slots=True)
class ParentFailure:
    error_code: str
    public_message: str


@dataclass(frozen=True, slots=True)
class ParentCancelled:
    analysis_id: str


type ParentChildResult = AnalyzeResponse | ParentFailure | ParentCancelled


def validate_child_result(
    expected_analysis_id: str, expected_schema_version: int, result: ChildAnalysisResult
) -> ParentChildResult:
    """Validate one child envelope before the parent interprets it."""
    if (
        result.analysis_id != expected_analysis_id
        or result.schema_version != expected_schema_version
    ):
        raise ValueError("child result does not match the expected analysis identity or schema")
    if isinstance(result, ChildAnalysisSuccess):
        response: AnalyzeResponse = _RESPONSE_ADAPTER.validate_json(result.response_json)
        if response.analysis_id != result.analysis_id:
            raise ValueError("child response analysis ID does not match its envelope")
        return response
    if isinstance(result, ChildAnalysisFailure):
        return ParentFailure(result.error_code, result.public_message)
    if isinstance(result, ChildAnalysisCancelled):
        return ParentCancelled(result.analysis_id)
    raise ValueError("child result has an unknown shape")


def _validate_reference(value: str) -> None:
    path = Path(value)
    if (
        not value
        or value != value.strip()
        or "\x00" in value
        or "/" in value
        or "\\" in value
        or path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or PureWindowsPath(value).drive
        or ".." in path.parts
        or value != path.name
    ):
        raise ValueError("video_reference must be a safe relative filename")


def _validate_version(value: int) -> None:
    if value != CHILD_ANALYSIS_SCHEMA_VERSION:
        raise ValueError("unsupported child analysis schema version")


def _validate_result(analysis_id: str, duration_ms: int, version: int) -> None:
    if not _SAFE_ID.fullmatch(analysis_id) or duration_ms < 0:
        raise ValueError("child result identity and duration must be safe")
    _validate_version(version)


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
