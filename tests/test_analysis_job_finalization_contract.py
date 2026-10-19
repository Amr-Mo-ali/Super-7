"""Database-free Slice 4 snapshot and fenced-success contract observations."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from test_analysis_job_worker_repository_contract import (
    _JOB,
    _LEASE,
    _NOW,
    _OTHER,
    _SLOT,
    _a,
    _done,
    _j,
    _Pool,
    _read,
    _settings,
    _terminal_absence,
    _write,
)
from test_analysis_stage_timing_contract import _response as _available_response

from adapters import psycopg_analysis_job_finalization_repository as finalization_adapter
from adapters.analysis_callback_presentation import callback_payload
from adapters.analysis_result_projection import convert_child_success
from adapters.psycopg_analysis_job_finalization_repository import (
    PsycopgAnalysisJobFinalizationRepository,
)
from domain.analysis_job_attempt import (
    AttemptAuthority,
    ClaimableAnalysisJob,
    LeaseId,
    WorkerClaim,
    WorkerSlotId,
)
from schemas.analysis import (
    AmbiguousResponse,
    CompletedResponse,
    Diagnostics,
    FeatureMetric,
    FeaturesResponse,
    NonCompletedResponse,
    TrackingResponse,
    VideoResponse,
)
from services.analysis_job_finalization import (
    AnalysisJobCompleted,
    AnalysisJobCompletionAlreadyApplied,
    AnalysisJobSuccessFinalizationRepository,
    FinalizableAnalysisResult,
    finalize_analysis_job_success,
)
from services.analysis_job_worker import ClaimLost
from services.process_contracts import ChildAnalysisSuccess

if TYPE_CHECKING:
    from adapters.psycopg_database import PsycopgDatabasePool

    def _real_pool_composition_witness(
        pool: PsycopgDatabasePool,
    ) -> AnalysisJobSuccessFinalizationRepository:
        repository: AnalysisJobSuccessFinalizationRepository = (
            PsycopgAnalysisJobFinalizationRepository(
                pool=pool,
                connection_acquire_timeout_seconds=1.25,
                operation_timeout_ms=4321,
                lock_timeout_ms=1234,
            )
        )
        return repository


_RAW_MODEL = "/models/player.pt+/models/ball.pt+bytetrack"
_SAFE_MODEL = "model_pair_v1"


def test_success_repository_does_not_import_worker_repository() -> None:
    tree = ast.parse(inspect.getsource(finalization_adapter))
    assert all(
        node.module != "adapters.psycopg_analysis_job_worker_repository"
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )


def _claim() -> WorkerClaim:
    authority = AttemptAuthority(_JOB, 2, WorkerSlotId(_SLOT), LeaseId(_LEASE))
    job = ClaimableAnalysisJob(
        _JOB,
        "Video_123",
        "Player_456",
        "match.mp4",
        "https://example.com/callback",
        "Version_1.0",
        3,
        _NOW,
    )
    return WorkerClaim(
        authority, job, _NOW - timedelta(seconds=10), _NOW, _NOW + timedelta(seconds=10)
    )


def _response(kind: str) -> CompletedResponse | AmbiguousResponse | NonCompletedResponse:
    if kind == "ambiguous":
        return AmbiguousResponse(
            analysis_id=str(_JOB),
            warnings=["private-warning"],
            status="ambiguous_target",
            candidate_count=2,
        )
    diagnostics = Diagnostics(
        frames_processed=2,
        frames_with_player_detections=0,
        total_person_detections=0,
        tracks_created=0,
        valid_candidate_tracks=0,
        ball_detections=0,
        trajectory_validation=[{"private": "/debug/file"}],
    )
    if kind == "completed":
        return CompletedResponse(
            analysis_id=str(_JOB),
            warnings=["private-warning"],
            status="completed",
            video=VideoResponse(duration_seconds=1, fps=2, width=4, height=4),
            selected_player=None,
            tracking=TrackingResponse(
                frames_processed=2, lost_track_count=0, longest_continuous_visible_segment=0
            ),
            features=FeaturesResponse(
                ball_proximity_time_seconds=FeatureMetric(),
                movement_intensity=FeatureMetric(),
                direction_changes=FeatureMetric(),
            ),
            scores=None,
            result_availability="UNAVAILABLE",
            unavailability_reason="target_not_established",
            diagnostics=diagnostics,
            analysis_version="Version_1.0",
            model_version=_RAW_MODEL,
            processing_time_ms=13,
            algorithm_versions={"player_detection": _RAW_MODEL, "tracking": "bytetrack_v0.1"},
            analysis_metadata={
                "video_hash": "a" * 64,
                "configuration_profile": "profile_v1",
                "pipeline_version": "pipeline_v1",
                "git_commit": None,
                "timestamp": "private-timestamp",
            },
            debug_artifacts={"trace": "/debug/file"},
            metadata={"secret": "private-metadata"},
        )
    return NonCompletedResponse(
        analysis_id=str(_JOB),
        warnings=["private-warning"],
        status="no_players_detected",
        diagnostics=diagnostics,
    )


def _result(kind: str = "noncompleted") -> FinalizableAnalysisResult:
    response = _response(kind)
    child = ChildAnalysisSuccess(
        str(_JOB), response.model_dump_json(), "Version_1.0", _RAW_MODEL, 17
    )
    return convert_child_success(
        child=child, response=response, claim=_claim(), model_catalog={_RAW_MODEL: _SAFE_MODEL}
    )


@pytest.mark.parametrize("kind", ("noncompleted", "ambiguous", "completed"))
def test_one_pure_conversion_preserves_legacy_callback_and_omits_diagnostics(kind: str) -> None:
    response = _response(kind)
    result = _result(kind)
    projected = json.loads(result.result_json)
    callback = json.loads(result.callback_json)
    assert callback == callback_payload("Video_123", "Player_456", response).model_dump(
        mode="json", by_alias=True
    )
    assert projected["analysis_id"] == callback["request_id"] == str(_JOB)
    assert callback["video_id"] == "Video_123" and callback["player_id"] == "Player_456"
    assert b"private-warning" not in result.result_json
    assert b"private-metadata" not in result.result_json
    assert b"/debug/file" not in result.result_json
    if kind == "completed":
        assert projected["model_version"] == _SAFE_MODEL
        assert projected["algorithm_versions"]["player_detection"] == _SAFE_MODEL
        assert set(projected["analysis_metadata"]) == {
            "video_hash",
            "configuration_profile",
            "pipeline_version",
            "git_commit",
        }


def test_available_completed_rating_callback_matches_legacy_mapping() -> None:
    response = _available_response(str(_JOB)).model_copy(
        update={"analysis_version": "Version_1.0", "model_version": _RAW_MODEL}
    )
    child = ChildAnalysisSuccess(
        str(_JOB), response.model_dump_json(), "Version_1.0", _RAW_MODEL, 17
    )
    frozen = convert_child_success(
        child=child, response=response, claim=_claim(), model_catalog={_RAW_MODEL: _SAFE_MODEL}
    )
    assert json.loads(frozen.callback_json) == callback_payload(
        "Video_123", "Player_456", response
    ).model_dump(mode="json", by_alias=True)


@pytest.mark.parametrize(
    "bad_bytes",
    (
        b'{"analysis_id":"x","analysis_id":"y"}',
        b'{"nested":{"a":1,"a":2}}',
        b'{"value":NaN}',
        b'{"value":"\\u0000"}',
        b"[]",
        b"\xff",
    ),
)
def test_malformed_frozen_snapshots_are_rejected_before_repository_io(bad_bytes: bytes) -> None:
    class RecordingRepository:
        calls = 0

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
            del authority, result
            self.calls += 1
            raise AssertionError("database interaction")

    repository = RecordingRepository()
    result = replace(_result(), result_json=bad_bytes)
    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            finalize_analysis_job_success(
                repository=repository, authority=_claim().authority, result=result
            )
        )
    assert repository.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("video_hash", "not-a-sha256"),
        ("video_hash", "A" * 64),
        ("configuration_profile", "../profile"),
        ("configuration_profile", ""),
        ("pipeline_version", "pipeline/path"),
        ("git_commit", "A" * 40),
        ("git_commit", 17),
        ("unapproved", "private"),
    ),
)
def test_retained_metadata_rejected_before_repository_io(field: str, value: object) -> None:
    class RecordingRepository:
        calls = 0

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
            del authority, result
            self.calls += 1
            raise AssertionError("database interaction")

    repository = RecordingRepository()
    result = _result("completed")
    projected = json.loads(result.result_json)
    projected["analysis_metadata"][field] = value
    malformed = replace(result, result_json=json.dumps(projected).encode())
    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            finalize_analysis_job_success(
                repository=repository, authority=_claim().authority, result=malformed
            )
        )
    assert repository.calls == 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("video_hash", "0" * 64),
        ("configuration_profile", "profile-1.0"),
        ("pipeline_version", "pipeline_2"),
        ("git_commit", "a" * 40),
        ("git_commit", None),
    ),
)
def test_retained_metadata_valid_values_and_omission_remain_accepted(
    field: str, value: object
) -> None:
    class RecordingRepository:
        calls = 0

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
            del result
            self.calls += 1
            return ClaimLost(authority)

    for omit in (False, True):
        repository = RecordingRepository()
        result = _result("completed")
        projected = json.loads(result.result_json)
        if omit:
            projected["analysis_metadata"].pop(field)
        else:
            projected["analysis_metadata"][field] = value
        supplied = replace(result, result_json=json.dumps(projected).encode())
        assert isinstance(
            asyncio.run(
                finalize_analysis_job_success(
                    repository=repository, authority=_claim().authority, result=supplied
                )
            ),
            ClaimLost,
        )
        assert repository.calls == 1


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("diagnostics", "frames_processed", True),
        ("diagnostics", "ball_analysis_quality", "high"),
        ("diagnostics", "private_extra", "path"),
        ("algorithm_versions", "tracking", 7),
    ),
)
def test_invalid_retained_typed_fields_are_rejected_before_io(
    section: str, field: str, value: object
) -> None:
    class RecordingRepository:
        calls = 0

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
            del authority, result
            self.calls += 1
            raise AssertionError("database interaction")

    repository = RecordingRepository()
    result = _result("completed")
    projected = json.loads(result.result_json)
    projected[section][field] = value
    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            finalize_analysis_job_success(
                repository=repository,
                authority=_claim().authority,
                result=replace(result, result_json=json.dumps(projected).encode()),
            )
        )
    assert repository.calls == 0


@pytest.mark.parametrize("section", ("analysis_metadata", "diagnostics"))
def test_retained_object_shape_is_rejected_before_io(section: str) -> None:
    class RecordingRepository:
        calls = 0

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
            del authority, result
            self.calls += 1
            raise AssertionError("database interaction")

    repository = RecordingRepository()
    result = _result("completed")
    projected = json.loads(result.result_json)
    projected[section] = []
    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            finalize_analysis_job_success(
                repository=repository,
                authority=_claim().authority,
                result=replace(result, result_json=json.dumps(projected).encode()),
            )
        )
    assert repository.calls == 0


@pytest.mark.parametrize(
    ("kind", "field", "value"),
    (
        ("ambiguous", "candidate_count", True),
        ("noncompleted", "candidate_count", "2"),
        ("noncompleted", "pipeline_state", []),
        ("completed", "pipeline_state", "VIDEO"),
        ("completed", "quality_gates", []),
    ),
)
def test_invalid_retained_variant_fields_are_rejected_before_io(
    kind: str, field: str, value: object
) -> None:
    class RecordingRepository:
        calls = 0

        async def finalize_success(
            self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
        ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost:
            del authority, result
            self.calls += 1
            raise AssertionError("database interaction")

    repository = RecordingRepository()
    result = _result(kind)
    projected = json.loads(result.result_json)
    projected[field] = value
    with pytest.raises((TypeError, ValueError)):
        asyncio.run(
            finalize_analysis_job_success(
                repository=repository,
                authority=_claim().authority,
                result=replace(result, result_json=json.dumps(projected).encode()),
            )
        )
    assert repository.calls == 0


def test_original_child_duplicate_keys_are_rejected_before_response_coercion() -> None:
    response = _response("noncompleted")
    raw = response.model_dump_json().replace('"status":', '"status":"failed","status":', 1)
    child = ChildAnalysisSuccess(str(_JOB), raw, "Version_1.0", _RAW_MODEL, 17)
    with pytest.raises(ValueError, match="Snapshot must be strict JSON") as captured:
        convert_child_success(
            child=child,
            response=response,
            claim=_claim(),
            model_catalog={_RAW_MODEL: _SAFE_MODEL},
        )
    assert isinstance(captured.value.__cause__, ValueError)
    assert str(captured.value.__cause__) == "Duplicate JSON object key."


def _terminal_rows(
    result: FinalizableAnalysisResult, finished: datetime
) -> tuple[tuple[object, ...], tuple[object, ...]]:
    terminal = (
        _JOB,
        2,
        "COMPLETED",
        finished,
        "analysis_terminal_v1",
        "analysis_result_v1",
        json.loads(result.result_json),
        result.model_version,
        result.processing_duration_ms,
        None,
        None,
    )
    outbox = (
        _OTHER,
        _JOB,
        "analysis_callback_snapshot_v1",
        json.loads(result.callback_json),
        "PENDING",
        finished,
        finished,
    )
    return terminal, outbox


def _repository(pool: _Pool) -> PsycopgAnalysisJobFinalizationRepository:
    return PsycopgAnalysisJobFinalizationRepository(
        pool=pool,
        connection_acquire_timeout_seconds=1.25,
        operation_timeout_ms=4321,
        lock_timeout_ms=1234,
    )


def test_fresh_success_writes_attempt_job_terminal_and_outbox_in_one_transaction() -> None:
    result = _result()
    finished = _NOW + timedelta(seconds=1)
    terminal, outbox = _terminal_rows(result, finished)
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j()], _JOB),
            _read("attempt", [_a()], _JOB, 2),
            _read("clock", [(_NOW,)]),
            *_terminal_absence(),
            _write(
                "attempt-write",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
                _SLOT,
                _LEASE,
            ),
            _write("job-write", [_j(state="COMPLETED")], _JOB),
            _write("terminal-write", [terminal], _JOB, 2, "COMPLETED"),
            _write("outbox-write", [outbox], _JOB, "PENDING"),
        ]
    )
    outcome = asyncio.run(
        _repository(pool).finalize_success(authority=_claim().authority, result=result)
    )
    assert outcome == AnalysisJobCompleted(_claim().authority, finished, _OTHER)
    assert pool.events.index("close:attempt-write") < pool.events.index("execute:job-write")
    assert pool.events.index("close:job-write") < pool.events.index("execute:terminal-write")
    assert pool.events.index("close:terminal-write") < pool.events.index("execute:outbox-write")
    _done(pool)


def test_exact_replay_keeps_first_event_and_uses_bound_jsonb_comparator() -> None:
    result = _result()
    finished = _NOW + timedelta(seconds=1)
    terminal, outbox = _terminal_rows(result, finished)
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j(state="COMPLETED")], _JOB),
            _read(
                "attempt",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
            ),
            _read("terminal", [terminal], _JOB),
            _read("outbox", [outbox], _JOB),
            _read(
                "equivalent",
                [(True,)],
                result.result_json.decode(),
                result.callback_json.decode(),
                _JOB,
            ),
        ]
    )
    outcome = asyncio.run(
        _repository(pool).finalize_success(authority=_claim().authority, result=result)
    )
    assert outcome == AnalysisJobCompletionAlreadyApplied(_claim().authority, finished, _OTHER)
    sql, parameters = pool.value.executions[-1][1:]
    assert "r.result_json - 'processing_time_ms'" in sql
    assert "%s::jsonb - 'processing_time_ms'" in sql
    assert parameters == (result.result_json.decode(), result.callback_json.decode(), _JOB)
    assert not any(label.endswith("-write") for label, _, _ in pool.value.executions)
    _done(pool)


def test_substantive_replay_conflict_is_claim_lost_without_writes() -> None:
    result = _result()
    finished = _NOW + timedelta(seconds=1)
    terminal, outbox = _terminal_rows(result, finished)
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j(state="COMPLETED")], _JOB),
            _read(
                "attempt",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
            ),
            _read("terminal", [terminal], _JOB),
            _read("outbox", [outbox], _JOB),
            _read("equivalent", [(False,)]),
        ]
    )
    outcome = asyncio.run(
        _repository(pool).finalize_success(authority=_claim().authority, result=result)
    )
    assert isinstance(outcome, ClaimLost)
    assert outcome.authority == _claim().authority
    assert not any(label.endswith("-write") for label, _, _ in pool.value.executions)
    _done(pool)


@pytest.mark.parametrize("boundary", ("stale-slot", "expired-lease"))
def test_stale_or_expired_success_authority_cannot_write(boundary: str) -> None:
    authority = _claim().authority
    steps = [*_settings(), _read("job", [_j()], _JOB), _read("attempt", [_a()], _JOB, 2)]
    if boundary == "stale-slot":
        authority = replace(authority, worker_slot_id=WorkerSlotId(_OTHER))
    else:
        steps.append(_read("clock", [(_NOW + timedelta(seconds=10),)]))
    pool = _Pool(steps)
    outcome = asyncio.run(_repository(pool).finalize_success(authority=authority, result=_result()))
    assert isinstance(outcome, ClaimLost) and outcome.authority == authority
    assert not any(label.endswith("-write") for label, _, _ in pool.value.executions)
    _done(pool)


def test_missing_terminal_evidence_precedes_stale_fence_classification() -> None:
    finished = _NOW + timedelta(seconds=1)
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j(state="COMPLETED")], _JOB),
            _read(
                "attempt",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
            ),
            *_terminal_absence(),
        ]
    )
    stale = replace(_claim().authority, worker_slot_id=WorkerSlotId(_OTHER))
    with pytest.raises(Exception) as captured:
        asyncio.run(_repository(pool).finalize_success(authority=stale, result=_result()))
    assert type(captured.value).__name__ == "AnalysisJobWorkerPersistenceInvariantError"
    assert not any(label.endswith("-write") for label, _, _ in pool.value.executions)
    _done(pool, rollback=True)


@pytest.mark.parametrize(
    "missing", ("terminal", "outbox", "malformed-terminal", "malformed-outbox")
)
def test_terminal_evidence_precedes_callback_association_conflict(missing: str) -> None:
    result = _result("completed")
    callback = json.loads(result.callback_json)
    callback["video_id"] = "DifferentVideo"
    mismatched = replace(result, callback_json=json.dumps(callback).encode())
    finished = _NOW + timedelta(seconds=1)
    terminal, outbox = _terminal_rows(result, finished)
    if missing == "malformed-terminal":
        malformed_terminal = list(terminal)
        malformed_terminal[4] = "wrong_schema"
        terminal = tuple(malformed_terminal)
    if missing == "malformed-outbox":
        malformed_outbox = list(outbox)
        malformed_outbox[2] = "wrong_schema"
        outbox = tuple(malformed_outbox)
    steps = [
        *_settings(),
        _read("job", [_j(state="COMPLETED")], _JOB),
        _read(
            "attempt",
            [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
            _JOB,
            2,
        ),
        _read("terminal", [] if missing == "terminal" else [terminal], _JOB),
        _read("outbox", [] if missing == "outbox" else [outbox], _JOB),
    ]
    pool = _Pool(steps)
    with pytest.raises(Exception) as captured:
        asyncio.run(
            _repository(pool).finalize_success(authority=_claim().authority, result=mismatched)
        )
    assert type(captured.value).__name__ == "AnalysisJobWorkerPersistenceInvariantError"
    observed = [label for label, _, _ in pool.value.executions]
    if missing == "malformed-terminal":
        assert observed[-1] == "terminal"
        assert "outbox" not in observed
        assert [step.label for step in pool.value.steps] == ["outbox"]
    else:
        assert observed[-2:] == ["terminal", "outbox"]
    _done(pool, rollback=True, remaining=missing == "malformed-terminal")


def test_sound_terminal_pair_precedes_callback_conflict_and_exact_replay() -> None:
    result = _result("completed")
    finished = _NOW + timedelta(seconds=1)
    terminal, outbox = _terminal_rows(result, finished)
    callback = json.loads(result.callback_json)
    callback["player_id"] = "DifferentPlayer"
    mismatched = replace(result, callback_json=json.dumps(callback).encode())
    for supplied, expected_replay in ((mismatched, False), (result, True)):
        steps = [
            *_settings(),
            _read("job", [_j(state="COMPLETED")], _JOB),
            _read(
                "attempt",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
            ),
            _read("terminal", [terminal], _JOB),
            _read("outbox", [outbox], _JOB),
        ]
        if expected_replay:
            steps.append(
                _read(
                    "equivalent",
                    [(True,)],
                    result.result_json.decode(),
                    result.callback_json.decode(),
                    _JOB,
                )
            )
        pool = _Pool(steps)
        outcome = asyncio.run(
            _repository(pool).finalize_success(authority=_claim().authority, result=supplied)
        )
        if expected_replay:
            assert outcome == AnalysisJobCompletionAlreadyApplied(
                _claim().authority, finished, _OTHER
            )
        else:
            assert isinstance(outcome, ClaimLost)
        labels = [label for label, _, _ in pool.value.executions]
        assert labels[2:6] == ["job", "attempt", "terminal", "outbox"]
        assert ("equivalent" in labels) is expected_replay
        _done(pool)


@pytest.mark.parametrize("status", ("completed", "failed"))
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("video_hash", "not-a-sha256"),
        ("configuration_profile", "../unsafe"),
        ("pipeline_version", ""),
        ("git_commit", "A" * 40),
        ("extra", "secret"),
    ),
)
def test_malformed_stored_metadata_precedes_replay_classification(
    status: str, field: str, value: object
) -> None:
    result = _result("completed")
    finished = _NOW + timedelta(seconds=1)
    terminal, outbox = _terminal_rows(result, finished)
    malformed = list(terminal)
    stored_result = malformed[6]
    assert isinstance(stored_result, dict)
    projected = dict(stored_result)
    projected["analysis_metadata"] = {
        **projected["analysis_metadata"],
        field: value,
    }
    malformed[6] = projected
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j(state="COMPLETED")], _JOB),
            _read(
                "attempt",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
            ),
            _read("terminal", [tuple(malformed)], _JOB),
            _read("outbox", [outbox], _JOB),
        ]
    )
    supplied = result
    if status == "failed":
        changed = json.loads(result.result_json)
        changed["video"]["fps"] = 3
        supplied = replace(result, result_json=json.dumps(changed).encode())
    with pytest.raises(Exception) as captured:
        asyncio.run(
            _repository(pool).finalize_success(authority=_claim().authority, result=supplied)
        )
    assert type(captured.value).__name__ == "AnalysisJobWorkerPersistenceInvariantError"
    assert "equivalent" not in [label for label, _, _ in pool.value.executions]
    _done(pool, rollback=True)


@pytest.mark.parametrize(
    ("field", "value"),
    (("frames_processed", True), ("ball_analysis_quality", "high"), ("extra", "private")),
)
def test_malformed_stored_diagnostics_precede_replay_q(field: str, value: object) -> None:
    result = _result("completed")
    finished = _NOW + timedelta(seconds=1)
    terminal, outbox = _terminal_rows(result, finished)
    malformed = list(terminal)
    stored_result = malformed[6]
    assert isinstance(stored_result, dict)
    projected = dict(stored_result)
    stored_diagnostics = projected["diagnostics"]
    assert isinstance(stored_diagnostics, dict)
    diagnostics = dict(stored_diagnostics)
    diagnostics[field] = value
    projected["diagnostics"] = diagnostics
    malformed[6] = projected
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j(state="COMPLETED")], _JOB),
            _read(
                "attempt",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
            ),
            _read("terminal", [tuple(malformed)], _JOB),
            _read("outbox", [outbox], _JOB),
        ]
    )
    with pytest.raises(Exception) as captured:
        asyncio.run(_repository(pool).finalize_success(authority=_claim().authority, result=result))
    assert type(captured.value).__name__ == "AnalysisJobWorkerPersistenceInvariantError"
    assert "equivalent" not in [label for label, _, _ in pool.value.executions]
    _done(pool, rollback=True)


def test_outbox_evidence_failure_rolls_back_all_prior_success_writes() -> None:
    result = _result()
    finished = _NOW + timedelta(seconds=1)
    terminal, _ = _terminal_rows(result, finished)
    pool = _Pool(
        [
            *_settings(),
            _read("job", [_j()], _JOB),
            _read("attempt", [_a()], _JOB, 2),
            _read("clock", [(_NOW,)]),
            *_terminal_absence(),
            _write(
                "attempt-write",
                [_a(status="SUCCEEDED", lease_expires_at=None, finished_at=finished)],
                _JOB,
                2,
                _SLOT,
                _LEASE,
            ),
            _write("job-write", [_j(state="COMPLETED")], _JOB),
            _write("terminal-write", [terminal], _JOB, 2, "COMPLETED"),
            _write("outbox-write", [], _JOB, "PENDING"),
        ]
    )
    with pytest.raises(Exception) as captured:
        asyncio.run(_repository(pool).finalize_success(authority=_claim().authority, result=result))
    assert type(captured.value).__name__ == "AnalysisJobWorkerPersistenceInvariantError"
    assert pool.events.count("rollback") == 1 and pool.events[-1] == "release"
    _done(pool, rollback=True)
