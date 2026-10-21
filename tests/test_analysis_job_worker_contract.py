"""RED-F only: public values, zero-I/O services, and admission alignment.

Authority: docs/workplans/sprint-2/04-worker-claim-lease-fencing-contract.md.
No persisted-attempt model is approved here: resolved row timestamp/nullability
checks are RED-G/H, and locking/expiry/reclaim evidence is RED-I. This file freezes
the ACTIVE WorkerClaim and the approved resolution outcomes without simulating
database transitions, leases, or a worker execution loop.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import inspect
import json
import sys
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from enum import Enum, StrEnum
from importlib import import_module
from pathlib import Path
from types import GenericAlias, ModuleType, UnionType
from typing import TYPE_CHECKING, Any, Protocol, Union, cast, get_args, get_origin, get_type_hints
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest
from pydantic import HttpUrl

from domain import analysis_job as durable
from services import analysis_job_admission as admission
from services.process_contracts import ChildAnalysisRequest

if TYPE_CHECKING:
    from domain.analysis_job_attempt import AttemptAuthority, LeaseDuration, WorkerSlotId
    from services.analysis_job_worker import (
        AnalysisJobWorkerRepository,
        claim_next_analysis_job,
        renew_analysis_job_lease,
    )

    async def _typecheck_public_port(
        repository: AnalysisJobWorkerRepository,
        slot: WorkerSlotId,
        lease: LeaseDuration,
        authority: AttemptAuthority,
    ) -> tuple[object, object]:
        return (
            await claim_next_analysis_job(
                repository=repository, worker_slot_id=slot, lease_duration=lease
            ),
            await renew_analysis_job_lease(
                repository=repository, authority=authority, lease_duration=lease
            ),
        )


_DOMAIN = "domain.analysis_job_attempt"
_SERVICE = "services.analysis_job_worker"
_JOB = UUID("12345678-1234-5678-1234-567812345678")
_SLOT = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_LEASE = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_TIME = datetime(2026, 10, 7, 12, tzinfo=UTC)
_CALLBACK = "https://example.com/callback"
_DIGEST = bytes.fromhex("b99dcef54edc40c22b91871a4c6838aa46abe5069b0abcc814d2188a4427b77b")
_ROOT = Path(__file__).resolve().parents[1]
_DOMAIN_EXPORTS = (
    "AttemptAuthority",
    "AttemptFailureCode",
    "AttemptStatus",
    "ClaimableAnalysisJob",
    "LeaseDuration",
    "LeaseId",
    "RecoveryBatchSize",
    "WorkerClaim",
    "WorkerSlotId",
)
_SERVICE_EXPORTS = (
    "AnalysisJobWorkerPersistenceInvariantError",
    "AnalysisJobWorkerPersistenceUnavailable",
    "AnalysisJobWorkerRepository",
    "AttemptExhausted",
    "AttemptFailed",
    "AttemptRequeued",
    "AttemptResolutionAlreadyApplied",
    "ClaimLost",
    "LeaseRenewed",
    "NoJobAvailable",
    "RecoverySummary",
    "WorkerSlotUnavailable",
    "claim_next_analysis_job",
    "finalize_retryable_failure",
    "finalize_terminal_failure",
    "recover_expired_analysis_jobs",
    "renew_analysis_job_lease",
)
_JOB_EXPORTS = (
    "AnalysisJob",
    "AnalysisJobAccepted",
    "AnalysisJobCapacityRejected",
    "AnalysisJobExisting",
    "AnalysisJobIdempotencyConflict",
    "AnalysisJobState",
    "NewAnalysisJob",
    "is_analysis_job_transition_allowed",
)


class _TextSubclass(str):
    pass


class _IntSubclass(int):
    pass


class _UuidSubclass(UUID):
    pass


class _DurationSubclass(timedelta):
    pass


class _UrlSubclass(HttpUrl):
    pass


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None

    def dst(self, dt: datetime | None) -> None:
        return None

    def tzname(self, dt: datetime | None) -> None:
        return None


class _FatalFailure(BaseException):
    pass


def _future(name: str) -> ModuleType:
    """Only absence of the requested future module is an intentional RED."""
    if name not in (_DOMAIN, _SERVICE):
        raise AssertionError("Not an approved future module")
    try:
        return import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        pytest.fail(f"RED-F missing future module: {name}", pytrace=False)


def _symbol(name: str) -> Any:
    module = _DOMAIN if name in _DOMAIN_EXPORTS else _SERVICE
    return getattr(_future(module), name)


def _claim_job(**overrides: object) -> Any:
    values: dict[str, object] = {
        "job_id": _JOB,
        "video_id": "video-123",
        "player_id": "player-456",
        "video_reference": "match-01.mp4",
        "callback_url": _CALLBACK,
        "analysis_version": "1.0.0",
        "attempt_limit": 3,
        "accepted_at": _TIME,
    }
    values.update(overrides)
    return _symbol("ClaimableAnalysisJob")(**values)


def _authority(**overrides: object) -> Any:
    values = {
        "job_id": _JOB,
        "attempt_number": 1,
        "worker_slot_id": _symbol("WorkerSlotId")(_SLOT),
        "lease_id": _symbol("LeaseId")(_LEASE),
    }
    values.update(overrides)
    return _symbol("AttemptAuthority")(**values)


def _claim(**overrides: object) -> Any:
    values = {
        "authority": _authority(),
        "job": _claim_job(),
        "started_at": _TIME,
        "heartbeat_at": _TIME,
        "lease_expires_at": _TIME + timedelta(seconds=1),
    }
    values.update(overrides)
    return _symbol("WorkerClaim")(**values)


def _outcome(name: str, **overrides: object) -> Any:
    # Fixture construction only; no state, time, or ownership decisions.
    shapes: dict[str, dict[str, object]] = {
        "NoJobAvailable": {},
        "WorkerSlotUnavailable": {"worker_slot_id": _symbol("WorkerSlotId")(_SLOT)},
        "ClaimLost": {"authority": _authority()},
        "LeaseRenewed": {
            "authority": _authority(),
            "heartbeat_at": _TIME,
            "lease_expires_at": _TIME + timedelta(seconds=1),
        },
        "AttemptRequeued": {"authority": _authority(), "attempt_limit": 3},
        "AttemptFailed": {
            "authority": _authority(),
            "failure_code": _symbol("AttemptFailureCode")("terminal"),
        },
        "AttemptExhausted": {
            "authority": _authority(),
            "attempt_limit": 3,
            "failure_code": _symbol("AttemptFailureCode")("retryable"),
        },
        "AttemptResolutionAlreadyApplied": {
            "authority": _authority(),
            "status": _symbol("AttemptStatus").RETRYABLE_FAILURE,
            "failure_code": _symbol("AttemptFailureCode")("retryable"),
        },
        "RecoverySummary": {"requeued_count": 0, "exhausted_count": 0},
    }
    values = shapes[name]
    values.update(overrides)
    return _symbol(name)(**values)


def _value(name: str, **overrides: object) -> Any:
    if name == "ClaimableAnalysisJob":
        return _claim_job(**overrides)
    if name == "AttemptAuthority":
        return _authority(**overrides)
    if name == "WorkerClaim":
        return _claim(**overrides)
    scalar: dict[str, object] = {
        "WorkerSlotId": _SLOT,
        "LeaseId": _LEASE,
        "LeaseDuration": timedelta(seconds=1),
        "RecoveryBatchSize": 1,
        "AttemptFailureCode": "retryable",
    }
    if name in scalar:
        return _symbol(name)(**({"value": scalar[name]} | overrides))
    return _outcome(name, **overrides)


def _durable_value(name: str, **overrides: object) -> object:
    # Explicit approved RED-H snapshots; these are fixtures, not runtime defaults.
    values: dict[str, object] = {
        "job_id": _JOB,
        "video_id": "video-123",
        "player_id": "player-456",
        "video_reference": "match-01.mp4",
        "callback_url": _CALLBACK,
        "analysis_version": "1.0.0",
        "attempt_limit": 3,
    }
    if name == "NewAnalysisJob":
        values.update(idempotency_lookup=b"protected", request_fingerprint=_DIGEST)
    else:
        values.update(state=durable.AnalysisJobState.QUEUED, accepted_at=_TIME)
    values.update(overrides)
    return cast(object, getattr(durable, name)(**values))


def _text_value(name: str, **overrides: object) -> Any:
    if name == "ClaimableAnalysisJob":
        return _claim_job(**overrides)
    return _durable_value(name, **overrides)


def _assert_fixed_failure(error: BaseException, message: str) -> None:
    assert error.args == (message,)
    assert error.__dict__ == {}
    # Even suppressed exception contexts must not retain rejected values.
    assert error.__cause__ is None
    assert error.__context__ is None


class _WorkerPortSpy:
    """Record one approved call and return/raise the supplied script verbatim."""

    def __init__(self, result: object = None, error: BaseException | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _record(self, name: str, arguments: dict[str, object]) -> object:
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result

    async def claim_next(self, *, worker_slot_id: object, lease_duration: object) -> object:
        return self._record(
            "claim_next", dict(worker_slot_id=worker_slot_id, lease_duration=lease_duration)
        )

    async def renew_lease(self, *, authority: object, lease_duration: object) -> object:
        return self._record("renew_lease", dict(authority=authority, lease_duration=lease_duration))

    async def finalize_retryable_failure(
        self, *, authority: object, failure_code: object
    ) -> object:
        return self._record(
            "finalize_retryable_failure", dict(authority=authority, failure_code=failure_code)
        )

    async def finalize_terminal_failure(self, *, authority: object, failure_code: object) -> object:
        return self._record(
            "finalize_terminal_failure", dict(authority=authority, failure_code=failure_code)
        )

    async def recover_expired(self, *, batch_size: object) -> object:
        return self._record("recover_expired", dict(batch_size=batch_size))


class _ProtectorSpy:
    def __init__(self, events: list[str], output: bytes = b"protected") -> None:
        self.events = events
        self.output = output
        self.calls: list[tuple[str, str]] = []

    def protect(self, *, caller_scope: str, raw_idempotency_key: str) -> bytes:
        self.events.append("protect")
        self.calls.append((caller_scope, raw_idempotency_key))
        return self.output


class _AdmissionPortSpy:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls: list[tuple[object, int]] = []
        self.result = durable.AnalysisJobCapacityRejected()

    async def accept_or_get(
        self, candidate: durable.NewAnalysisJob, *, max_queue_size: int
    ) -> durable.AnalysisJobCapacityRejected:
        self.events.append("repository")
        self.calls.append((candidate, max_queue_size))
        return self.result


def _admit(repository: _AdmissionPortSpy, protector: _ProtectorSpy, **overrides: object) -> object:
    values: dict[str, object] = {
        "repository": repository,
        "protector": protector,
        "job_id": _JOB,
        "caller_scope": "apex:tenant-123",
        "raw_idempotency_key": "opaque-key",
        "video_id": "video-123",
        "player_id": "player-456",
        "video_reference": "match-01.mp4",
        "callback_url": HttpUrl(_CALLBACK),
        "analysis_version": "1.0.0",
        "attempt_limit": 3,
        "max_queue_size": 3,
    }
    values.update(overrides)
    return asyncio.run(admission.admit_analysis_job(**cast(Any, values)))


_OPERATIONS = (
    (
        "claim_next_analysis_job",
        "claim_next",
        ("worker_slot_id", "lease_duration"),
        ("WorkerClaim", "NoJobAvailable", "WorkerSlotUnavailable"),
    ),
    (
        "renew_analysis_job_lease",
        "renew_lease",
        ("authority", "lease_duration"),
        ("LeaseRenewed", "ClaimLost"),
    ),
    (
        "finalize_retryable_failure",
        "finalize_retryable_failure",
        ("authority", "failure_code"),
        ("AttemptRequeued", "AttemptExhausted", "AttemptResolutionAlreadyApplied", "ClaimLost"),
    ),
    (
        "finalize_terminal_failure",
        "finalize_terminal_failure",
        ("authority", "failure_code"),
        ("AttemptFailed", "AttemptResolutionAlreadyApplied", "ClaimLost"),
    ),
    ("recover_expired_analysis_jobs", "recover_expired", ("batch_size",), ("RecoverySummary",)),
)


def _operation_arguments(names: tuple[str, ...]) -> dict[str, object]:
    values = {
        "worker_slot_id": _value("WorkerSlotId"),
        "lease_duration": _value("LeaseDuration"),
        "authority": _authority(),
        "failure_code": _value("AttemptFailureCode"),
        "batch_size": _value("RecoveryBatchSize"),
    }
    return {name: values[name] for name in names}


# Helper guards exercise failure classification without importing future code.
@pytest.mark.parametrize("module_name", (_DOMAIN, _SERVICE))
def test_helper_exact_future_absence_is_red(
    module_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(name: str) -> ModuleType:
        raise ModuleNotFoundError("missing", name=name)

    monkeypatch.setattr(sys.modules[__name__], "import_module", missing)
    with pytest.raises(
        pytest.fail.Exception, match=f"^RED-F missing future module: {module_name}$"
    ):
        _future(module_name)


@pytest.mark.parametrize(
    "error",
    (
        ModuleNotFoundError("dependency missing", name="transitive_dependency"),
        SyntaxError("syntax defect"),
        ImportError("import defect"),
        AttributeError("symbol defect"),
    ),
)
def test_helper_import_defects_propagate(error: Exception, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str) -> ModuleType:
        raise error

    monkeypatch.setattr(sys.modules[__name__], "import_module", broken)
    with pytest.raises(type(error)) as caught:
        _future(_DOMAIN)
    assert caught.value is error


def test_helper_missing_symbol_is_not_module_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "import_module", lambda name: ModuleType(name))
    with pytest.raises(AttributeError):
        _symbol("WorkerSlotId")


def test_helper_worker_spy_preserves_call_result_and_error() -> None:
    result = object()
    spy = _WorkerPortSpy(result)
    slot, lease = object(), object()
    assert asyncio.run(spy.claim_next(worker_slot_id=slot, lease_duration=lease)) is result
    assert spy.calls == [("claim_next", {"worker_slot_id": slot, "lease_duration": lease})]
    error = RuntimeError("test script")
    spy = _WorkerPortSpy(error=error)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(spy.recover_expired(batch_size=1))
    assert caught.value is error
    assert spy.calls == [("recover_expired", {"batch_size": 1})]


@pytest.mark.parametrize(
    ("module_name", "exports"),
    (
        (_DOMAIN, _DOMAIN_EXPORTS),
        (_SERVICE, _SERVICE_EXPORTS),
        ("domain.analysis_job", _JOB_EXPORTS),
    ),
)
def test_public_exports_are_exact(module_name: str, exports: tuple[str, ...]) -> None:
    module = (
        import_module(module_name) if module_name == "domain.analysis_job" else _future(module_name)
    )
    assert tuple(module.__all__) == exports
    assert all(hasattr(module, name) for name in exports)


_SHAPES = {
    "WorkerSlotId": (("value", "UUID"),),
    "LeaseId": (("value", "UUID"),),
    "LeaseDuration": (("value", "timedelta"),),
    "RecoveryBatchSize": (("value", "int"),),
    "AttemptFailureCode": (("value", "str"),),
    "AttemptAuthority": (
        ("job_id", "UUID"),
        ("attempt_number", "int"),
        ("worker_slot_id", "WorkerSlotId"),
        ("lease_id", "LeaseId"),
    ),
    "ClaimableAnalysisJob": (
        ("job_id", "UUID"),
        ("video_id", "str"),
        ("player_id", "str"),
        ("video_reference", "str"),
        ("callback_url", "str"),
        ("analysis_version", "str"),
        ("attempt_limit", "int"),
        ("accepted_at", "datetime"),
    ),
    "WorkerClaim": (
        ("authority", "AttemptAuthority"),
        ("job", "ClaimableAnalysisJob"),
        ("started_at", "datetime"),
        ("heartbeat_at", "datetime"),
        ("lease_expires_at", "datetime"),
    ),
    "NoJobAvailable": (),
    "WorkerSlotUnavailable": (("worker_slot_id", "WorkerSlotId"),),
    "ClaimLost": (("authority", "AttemptAuthority"),),
    "LeaseRenewed": (
        ("authority", "AttemptAuthority"),
        ("heartbeat_at", "datetime"),
        ("lease_expires_at", "datetime"),
    ),
    "AttemptRequeued": (("authority", "AttemptAuthority"), ("attempt_limit", "int")),
    "AttemptFailed": (("authority", "AttemptAuthority"), ("failure_code", "AttemptFailureCode")),
    "AttemptExhausted": (
        ("authority", "AttemptAuthority"),
        ("attempt_limit", "int"),
        ("failure_code", "AttemptFailureCode"),
    ),
    "AttemptResolutionAlreadyApplied": (
        ("authority", "AttemptAuthority"),
        ("status", "AttemptStatus"),
        ("failure_code", "AttemptFailureCode"),
    ),
    "RecoverySummary": (("requeued_count", "int"), ("exhausted_count", "int")),
}


@pytest.mark.parametrize("name", tuple(_SHAPES))
def test_public_value_fields_annotations_and_immutability(name: str) -> None:
    cls = _symbol(name)
    value = _value(name)
    shape = _SHAPES[name]
    builtin_types: dict[str, object] = {
        "UUID": UUID,
        "timedelta": timedelta,
        "datetime": datetime,
        "int": int,
        "str": str,
    }
    expected = {
        field: builtin_types[type_name] if type_name in builtin_types else _symbol(type_name)
        for field, type_name in shape
    }
    assert is_dataclass(cast(object, cls))
    assert cls.__dataclass_params__.frozen is True
    assert tuple(item.name for item in fields(value)) == tuple(expected)
    assert get_type_hints(cls) == expected
    assert tuple(inspect.signature(cls).parameters) == tuple(expected)
    assert all(
        p.default is inspect.Parameter.empty for p in inspect.signature(cls).parameters.values()
    )
    assert not hasattr(value, "__dict__")
    assert tuple(cls.__slots__) == tuple(expected)
    with pytest.raises((AttributeError, TypeError)):
        setattr(value, next(iter(expected), "unexpected"), None)
    with pytest.raises((AttributeError, TypeError)):
        value.unexpected = True


def test_status_vocabulary_and_four_part_authority_are_exact() -> None:
    status = _symbol("AttemptStatus")
    assert issubclass(status, StrEnum)
    names = (
        "ACTIVE",
        "INTERRUPTED",
        "RETRYABLE_FAILURE",
        "TERMINAL_FAILURE",
        "EXHAUSTED",
        "SUCCEEDED",
    )
    assert tuple(status.__members__) == names
    assert tuple(member.value for member in status) == names
    authority = _authority()
    assert (
        authority.job_id,
        authority.attempt_number,
        authority.worker_slot_id.value,
        authority.lease_id.value,
    ) == (_JOB, 1, _SLOT, _LEASE)
    assert type(authority.worker_slot_id) is not type(authority.lease_id)
    assert authority.worker_slot_id.value is _SLOT
    assert authority.lease_id.value is _LEASE
    # A slot can be retained across claims; leases remain distinct typed values.
    next_job_id = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
    next_lease_id = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    next_lease = _symbol("LeaseId")(next_lease_id)
    next_authority = _authority(
        job_id=next_job_id,
        attempt_number=2,
        worker_slot_id=authority.worker_slot_id,
        lease_id=next_lease,
    )
    assert (
        next_authority.job_id,
        next_authority.attempt_number,
        next_authority.worker_slot_id,
        next_authority.lease_id,
    ) == (next_job_id, 2, authority.worker_slot_id, next_lease)
    assert next_authority.job_id is next_job_id
    assert next_authority.worker_slot_id is authority.worker_slot_id
    assert next_authority.worker_slot_id.value is _SLOT
    assert next_authority.lease_id is next_lease
    assert next_authority.lease_id.value is next_lease_id
    assert next_authority.lease_id != authority.lease_id


@pytest.mark.parametrize("name", ("WorkerSlotId", "LeaseId"))
def test_uuid_wrappers_preserve_supplied_uuid_object(name: str) -> None:
    supplied = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
    assert _value(name, value=supplied).value is supplied


@pytest.mark.parametrize("name", ("NewAnalysisJob", "AnalysisJob", "ClaimableAnalysisJob"))
def test_job_payload_preserves_valid_uuid_and_has_no_sensitive_fields(name: str) -> None:
    value = _text_value(name)
    assert value.job_id is _JOB
    forbidden = {
        "raw_idempotency_key",
        "caller_scope",
        "hmac_secret",
        "connection",
        "cursor",
        "process",
        "task",
        "result",
        "outbox",
    }
    assert forbidden.isdisjoint(item.name for item in fields(value))
    if name == "ClaimableAnalysisJob":
        assert {"idempotency_lookup", "request_fingerprint"}.isdisjoint(
            item.name for item in fields(value)
        )


@pytest.mark.parametrize("name", ("NewAnalysisJob", "AnalysisJob"))
def test_red_h_domain_shape_requires_exact_policy_snapshots(name: str) -> None:
    value = _durable_value(name)
    assert is_dataclass(value)
    cls = type(value)
    expected: dict[str, object] = {"job_id": UUID}
    if name == "NewAnalysisJob":
        expected.update(idempotency_lookup=bytes, request_fingerprint=bytes)
    expected.update(video_id=str, player_id=str, video_reference=str, callback_url=str)
    expected.update(analysis_version=str)
    if name == "AnalysisJob":
        expected.update(state=durable.AnalysisJobState)
    expected.update(attempt_limit=int)
    if name == "AnalysisJob":
        expected.update(accepted_at=datetime)
    assert tuple(item.name for item in fields(value)) == tuple(expected)
    assert get_type_hints(cls) == expected
    assert tuple(inspect.signature(cls).parameters) == tuple(expected)
    assert not hasattr(value, "__dict__")
    assert cast(Any, cls).__dataclass_params__.frozen is True
    assert all(
        inspect.signature(cls).parameters[field].default is inspect.Parameter.empty
        for field in ("analysis_version", "attempt_limit")
    )


@pytest.mark.parametrize("name", ("NewAnalysisJob", "AnalysisJob"))
@pytest.mark.parametrize(
    ("field", "bad", "message", "error_type"),
    (
        ("analysis_version", None, "Admission identifiers must be strings.", TypeError),
        (
            "analysis_version",
            _TextSubclass("v1"),
            "Admission identifiers must be strings.",
            TypeError,
        ),
        *(
            (
                "analysis_version",
                value,
                "Analysis version must have bounded safe syntax.",
                ValueError,
            )
            for value in ("", "v" * 65, "_v", "é", "v+1", "v\n")
        ),
        *(
            ("attempt_limit", value, "Policy value must be an exact integer.", TypeError)
            for value in (True, _IntSubclass(3), 3.0, None)
        ),
        *(
            ("attempt_limit", value, "Policy value must be positive.", ValueError)
            for value in (0, -1)
        ),
    ),
)
def test_red_h_snapshot_validation_matches_worker_payload(
    name: str, field: str, bad: object, message: str, error_type: type[Exception]
) -> None:
    with pytest.raises(error_type) as caught:
        _durable_value(name, **{field: bad})
    _assert_fixed_failure(caught.value, message)


@pytest.mark.parametrize("version", ("1.0.0", "A" + "." * 63, "Version_1-2.0"))
@pytest.mark.parametrize("name", ("NewAnalysisJob", "AnalysisJob"))
def test_red_h_snapshot_values_are_preserved_without_public_count(name: str, version: str) -> None:
    value = cast(Any, _durable_value(name, analysis_version=version, attempt_limit=2**63))
    assert value.analysis_version is version and value.attempt_limit == 2**63
    assert not hasattr(value, "attempt_count")


@pytest.mark.parametrize(
    ("field", "bad", "message", "error_type"),
    (
        ("analysis_version", None, "Admission identifiers must be strings.", TypeError),
        (
            "analysis_version",
            "bad+version",
            "Analysis version must have bounded safe syntax.",
            ValueError,
        ),
        ("attempt_limit", True, "Policy value must be an exact integer.", TypeError),
        ("attempt_limit", 0, "Policy value must be positive.", ValueError),
    ),
)
def test_red_h_admission_snapshot_validation_precedes_all_boundary_effects(
    field: str,
    bad: object,
    message: str,
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    repository, protector = _AdmissionPortSpy(events), _ProtectorSpy(events)

    def forbidden_fingerprint(**kwargs: object) -> bytes:
        events.append("fingerprint")
        raise AssertionError("Invalid policy reached fingerprinting")

    monkeypatch.setattr(admission, "fingerprint_analysis_request", forbidden_fingerprint)
    with pytest.raises(error_type) as caught:
        _admit(repository, protector, **{field: bad})
    _assert_fixed_failure(caught.value, message)
    assert events == [] and repository.calls == [] and protector.calls == []


def test_red_h_policy_snapshots_do_not_change_fingerprint_v1_or_protected_lookup() -> None:
    candidates: list[durable.NewAnalysisJob] = []
    for version, limit in (("1.0.0", 3), ("Version_2.0", 7)):
        events: list[str] = []
        repository, protector = _AdmissionPortSpy(events), _ProtectorSpy(events)
        assert (
            _admit(repository, protector, analysis_version=version, attempt_limit=limit)
            is repository.result
        )
        assert events == ["protect", "repository"]
        candidate, _ = repository.calls[0]
        assert (
            cast(Any, candidate).analysis_version == version
            and cast(Any, candidate).attempt_limit == limit
        )
        candidates.append(cast(durable.NewAnalysisJob, candidate))
    assert candidates[0].request_fingerprint == candidates[1].request_fingerprint == _DIGEST
    assert candidates[0].idempotency_lookup == candidates[1].idempotency_lookup == b"protected"


@pytest.mark.parametrize("field", ("authority", "job"))
@pytest.mark.parametrize("bad", (None, "untyped", object()))
def test_active_claim_requires_typed_authority_and_job(field: str, bad: object) -> None:
    _future(_DOMAIN)
    with pytest.raises((TypeError, ValueError)):
        _claim(**{field: bad})


_BAD_UUIDS = (
    pytest.param(UUID(int=0), id="nil"),
    pytest.param(str(_JOB), id="text"),
    pytest.param(_JOB.bytes, id="bytes"),
    pytest.param(1, id="int"),
    pytest.param(None, id="none"),
    pytest.param(object(), id="object"),
    pytest.param(_UuidSubclass(str(_JOB)), id="subclass"),
)


@pytest.mark.parametrize(
    ("name", "field"),
    (
        ("WorkerSlotId", "value"),
        ("LeaseId", "value"),
        ("AttemptAuthority", "job_id"),
        ("ClaimableAnalysisJob", "job_id"),
    ),
)
@pytest.mark.parametrize("bad", _BAD_UUIDS)
def test_future_uuid_fields_reject_nonexact_and_nil(name: str, field: str, bad: object) -> None:
    factory = _symbol(name) if field == "value" else (lambda **kw: _value(name, **kw))
    with pytest.raises((TypeError, ValueError)):
        factory(**{field: bad})


@pytest.mark.parametrize("name", ("NewAnalysisJob", "AnalysisJob"))
@pytest.mark.parametrize("bad", _BAD_UUIDS)
def test_existing_domain_uuid_fields_reject_nonexact_and_nil(name: str, bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _durable_value(name, job_id=bad)


@pytest.mark.parametrize("bad", _BAD_UUIDS)
def test_existing_conflict_uuid_rejects_nonexact_and_nil(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        durable.AnalysisJobIdempotencyConflict(cast(UUID, bad))


def test_guard_conflict_preserves_valid_uuid_object() -> None:
    assert durable.AnalysisJobIdempotencyConflict(_JOB).existing_job_id is _JOB


@pytest.mark.parametrize(
    ("field", "bad"),
    (("worker_slot_id", _SLOT), ("lease_id", _LEASE), ("worker_slot_id", None), ("lease_id", None)),
)
def test_authority_requires_typed_slot_and_lease(field: str, bad: object) -> None:
    _future(_DOMAIN)
    with pytest.raises((TypeError, ValueError)):
        _authority(**{field: bad})


def test_authority_does_not_interchange_slot_and_lease() -> None:
    slot, lease = _value("WorkerSlotId"), _value("LeaseId")
    with pytest.raises((TypeError, ValueError)):
        _authority(worker_slot_id=lease)
    with pytest.raises((TypeError, ValueError)):
        _authority(lease_id=slot)


_BAD_POSITIVE_INTS = (
    pytest.param(True, id="bool"),
    pytest.param(0, id="zero"),
    pytest.param(-1, id="negative"),
    pytest.param(1.0, id="float"),
    pytest.param(None, id="none"),
    pytest.param(_IntSubclass(1), id="subclass"),
)


@pytest.mark.parametrize(
    ("name", "field"),
    (
        ("RecoveryBatchSize", "value"),
        ("AttemptAuthority", "attempt_number"),
        ("ClaimableAnalysisJob", "attempt_limit"),
        ("AttemptRequeued", "attempt_limit"),
        ("AttemptExhausted", "attempt_limit"),
    ),
)
@pytest.mark.parametrize("bad", _BAD_POSITIVE_INTS)
def test_positive_integer_fields_are_exact(name: str, field: str, bad: object) -> None:
    _symbol(name)
    with pytest.raises((TypeError, ValueError)):
        _value(name, **{field: bad})


@pytest.mark.parametrize("value", (1, 2, 2**63))
def test_positive_policy_and_batch_have_no_hidden_maximum(value: int) -> None:
    assert _value("RecoveryBatchSize", value=value).value == value
    assert _claim_job(attempt_limit=value).attempt_limit == value


@pytest.mark.parametrize(
    "bad", (timedelta(0), timedelta(seconds=-1), 1, True, None, _DurationSubclass(seconds=1))
)
def test_lease_duration_is_exact_and_positive(bad: object) -> None:
    cls = _symbol("LeaseDuration")
    with pytest.raises((TypeError, ValueError)):
        cls(bad)


@pytest.mark.parametrize("value", (timedelta(microseconds=1), timedelta(days=1000)))
def test_lease_duration_has_no_hidden_minimum_or_maximum(value: timedelta) -> None:
    assert _value("LeaseDuration", value=value).value is value


@pytest.mark.parametrize(
    ("name", "valid"),
    (
        ("AttemptFailureCode", "a"),
        ("AttemptFailureCode", "a" + "_" * 63),
        ("AttemptFailureCode", "video_unavailable"),
        ("ClaimableAnalysisJob", "1.0.0"),
        ("ClaimableAnalysisJob", "A" + "." * 63),
        ("ClaimableAnalysisJob", "Version_1-2.0"),
    ),
)
def test_policy_and_failure_code_accept_exact_bounded_syntax(name: str, valid: str) -> None:
    if name == "AttemptFailureCode":
        assert _value(name, value=valid).value == valid
    else:
        assert _claim_job(analysis_version=valid).analysis_version == valid


@pytest.mark.parametrize(
    "bad", (None, 1, _TextSubclass("safe"), "", "a" * 65, " safe", "safe ", "safe\n", "é", "a\x00")
)
@pytest.mark.parametrize("name", ("AttemptFailureCode", "ClaimableAnalysisJob"))
def test_policy_and_failure_code_reject_unsafe_syntax(name: str, bad: object) -> None:
    _symbol(name)
    field = "value" if name == "AttemptFailureCode" else "analysis_version"
    with pytest.raises((TypeError, ValueError)):
        _value(name, **{field: bad})


@pytest.mark.parametrize("bad", ("Upper", "1failure", "a-b", "a.b", "error:detail"))
def test_failure_code_has_no_freeform_or_uppercase_syntax(bad: str) -> None:
    cls = _symbol("AttemptFailureCode")
    with pytest.raises((TypeError, ValueError)):
        cls(bad)


@pytest.mark.parametrize(
    "bad", ("_version", ".version", "-version", "v+metadata", "commit message")
)
def test_version_requires_alphanumeric_start_and_no_metadata(bad: str) -> None:
    _future(_DOMAIN)
    with pytest.raises((TypeError, ValueError)):
        _claim_job(analysis_version=bad)


_TEXT_TYPES = ("NewAnalysisJob", "AnalysisJob", "ClaimableAnalysisJob")
_TEXT_FIELDS = ("video_id", "player_id", "video_reference", "callback_url")
_BAD_TEXT_TYPES = (
    pytest.param(None, id="none"),
    pytest.param(3, id="integer"),
    pytest.param(b"safe", id="bytes"),
    pytest.param(object(), id="object"),
    pytest.param(_TextSubclass("safe"), id="subclass"),
)
_UNSAFE_IDS = (
    pytest.param("", id="empty"),
    pytest.param(" video", id="leading-space"),
    pytest.param("video ", id="trailing-space"),
    pytest.param("a b", id="internal-space"),
    pytest.param("a\n", id="newline"),
    pytest.param("é", id="unicode"),
    pytest.param("a\x00", id="nul"),
    pytest.param("a/b", id="slash"),
    pytest.param("a\\b", id="backslash"),
    pytest.param("a.b", id="dot"),
    pytest.param("a:b", id="colon"),
    pytest.param("\ud800", id="surrogate"),
)
_UNSAFE_REFERENCES = (
    pytest.param("", id="empty"),
    pytest.param(" file.mp4", id="leading-space"),
    pytest.param("file.mp4 ", id="trailing-space"),
    pytest.param("file.mp4\n", id="trailing-newline"),
    pytest.param("file\x00.mp4", id="nul"),
    pytest.param("/file.mp4", id="posix-absolute"),
    pytest.param("dir/file.mp4", id="slash"),
    pytest.param("dir\\file.mp4", id="backslash"),
    pytest.param("C:\\file.mp4", id="windows-absolute"),
    pytest.param("C:/file.mp4", id="windows-forward"),
    pytest.param("C:file.mp4", id="drive-relative"),
    pytest.param("C:", id="drive-only"),
    pytest.param("\\\\host\\share\\file.mp4", id="unc"),
    pytest.param("//host/share/file.mp4", id="unc-forward"),
    pytest.param(".", id="dot"),
    pytest.param("..", id="dotdot"),
    pytest.param("./file.mp4", id="dot-component"),
    pytest.param("dir/../file.mp4", id="parent-component"),
    pytest.param("dir//file.mp4", id="repeated-separator"),
    pytest.param("file.mp4/", id="trailing-separator"),
    pytest.param("file.mp4\\", id="trailing-backslash"),
)
_NONCANONICAL_CALLBACKS = (
    pytest.param("", id="empty"),
    pytest.param("not-a-url", id="invalid"),
    pytest.param("ftp://example.com/a", id="scheme"),
    pytest.param("HTTPS://EXAMPLE.COM/callback", id="case"),
    pytest.param("https://example.com:443/callback", id="default-port"),
    pytest.param("https://example.com", id="absent-path"),
    pytest.param(" https://example.com/callback ", id="whitespace"),
    pytest.param("https://example.com/résultat", id="unicode-path"),
    pytest.param("https://éxample.com/callback", id="unicode-host"),
    pytest.param("https://example.com/\ud800", id="surrogate"),
)


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize("field", _TEXT_FIELDS)
@pytest.mark.parametrize("bad", _BAD_TEXT_TYPES)
def test_durable_text_requires_exact_builtin_str(name: str, field: str, bad: object) -> None:
    if name == "ClaimableAnalysisJob":
        _future(_DOMAIN)
    with pytest.raises(TypeError) as caught:
        _text_value(name, **{field: bad})
    _assert_fixed_failure(caught.value, "Admission identifiers must be strings.")


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize("field", ("video_id", "player_id"))
@pytest.mark.parametrize("bad", _UNSAFE_IDS)
def test_durable_ids_freeze_safe_ascii_syntax(name: str, field: str, bad: str) -> None:
    if name == "ClaimableAnalysisJob":
        _future(_DOMAIN)
    with pytest.raises(ValueError) as caught:
        _text_value(name, **{field: bad})
    _assert_fixed_failure(caught.value, "child analysis identifiers must be safe")


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize("bad", _UNSAFE_REFERENCES)
def test_durable_reference_freezes_native_and_windows_path_rejection(name: str, bad: str) -> None:
    if name == "ClaimableAnalysisJob":
        _future(_DOMAIN)
    with pytest.raises(ValueError) as caught:
        _text_value(name, video_reference=bad)
    _assert_fixed_failure(caught.value, "video_reference must be a safe relative filename")


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize("bad", _NONCANONICAL_CALLBACKS)
def test_durable_callback_requires_nonempty_canonical_text(name: str, bad: str) -> None:
    if name == "ClaimableAnalysisJob":
        _future(_DOMAIN)
    with pytest.raises(ValueError) as caught:
        _text_value(name, callback_url=bad)
    _assert_fixed_failure(caught.value, "Callback URL is invalid.")


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize("fragment", ("#", "#private-fragment"))
def test_durable_callback_fragment_has_existing_fixed_failure(name: str, fragment: str) -> None:
    if name == "ClaimableAnalysisJob":
        _future(_DOMAIN)
    with pytest.raises(ValueError) as caught:
        _text_value(name, callback_url=_CALLBACK + fragment)
    _assert_fixed_failure(caught.value, "Callback URL must not contain a fragment.")


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize(
    "reference",
    (
        "Match-é.MP4",
        "match 01.mp4",
        "foo:bar.mp4",
        ":",
        "foo..bar.mp4",
        "file\nname.mp4",
        "file\ud800.mp4",
    ),
)
def test_durable_filename_preserves_every_accepted_spelling(name: str, reference: str) -> None:
    value = _text_value(
        name, video_id="Video_ABC-123", player_id="Player_ABC-456", video_reference=reference
    )
    assert value.video_id == "Video_ABC-123"
    assert value.player_id == "Player_ABC-456"
    assert value.video_reference is reference
    # Compare directly to the existing child predicate, including colon/Unicode.
    child = ChildAnalysisRequest(str(_JOB), value.video_id, value.player_id, reference)
    assert child.video_reference is reference


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize(
    "url",
    (
        "http://example.com/",
        "https://example.com/r%C3%A9sultat",
        "https://example.com/a%2fb",
        "https://example.com/callback?b=2&a=1",
    ),
)
def test_durable_callback_preserves_valid_canonical_spelling(name: str, url: str) -> None:
    value = _text_value(name, callback_url=url)
    assert value.callback_url is url


@pytest.mark.parametrize("cls", (HttpUrl, _UrlSubclass))
def test_guard_canonicalization_accepts_validated_httpurl_and_subclasses(
    cls: type[HttpUrl],
) -> None:
    url = cls("HTTPS://EXAMPLE.COM:443/callback")
    assert admission.canonicalize_callback_url(url) is url
    assert str(url) == _CALLBACK


@pytest.mark.parametrize("bad", (None, _CALLBACK, 1, object()))
def test_guard_other_canonicalization_inputs_have_existing_fixed_failure(bad: object) -> None:
    with pytest.raises(TypeError) as caught:
        admission.canonicalize_callback_url(cast(HttpUrl, bad))
    _assert_fixed_failure(caught.value, "Callback URL must be validated before canonicalization.")


@pytest.mark.parametrize("cls", (HttpUrl, _UrlSubclass))
@pytest.mark.parametrize("fragment", ("#", "#private-fragment"))
def test_guard_fragment_rejection_includes_httpurl_subclasses(
    cls: type[HttpUrl], fragment: str
) -> None:
    with pytest.raises(ValueError) as caught:
        admission.canonicalize_callback_url(cls(_CALLBACK + fragment))
    _assert_fixed_failure(caught.value, "Callback URL must not contain a fragment.")


@pytest.mark.parametrize("name", _TEXT_TYPES)
@pytest.mark.parametrize("cls", (HttpUrl, _UrlSubclass))
def test_stored_callback_does_not_accept_url_objects(name: str, cls: type[HttpUrl]) -> None:
    if name == "ClaimableAnalysisJob":
        _future(_DOMAIN)
    with pytest.raises(TypeError) as caught:
        _text_value(name, callback_url=cls(_CALLBACK))
    _assert_fixed_failure(caught.value, "Admission identifiers must be strings.")


@pytest.mark.parametrize(
    ("field", "bad", "message", "error_type"),
    tuple(
        (field, param.values[0], "child analysis identifiers must be safe", ValueError)
        for field in ("video_id", "player_id")
        for param in _UNSAFE_IDS
    )
    + tuple(
        (
            "video_reference",
            param.values[0],
            "video_reference must be a safe relative filename",
            ValueError,
        )
        for param in _UNSAFE_REFERENCES
    ),
    ids=[f"{field}-{param.id}" for field in ("video_id", "player_id") for param in _UNSAFE_IDS]
    + [f"reference-{param.id}" for param in _UNSAFE_REFERENCES],
)
def test_existing_admission_rejects_unsafe_text_before_protector_fingerprint_io(
    field: str,
    bad: object,
    message: str,
    error_type: type[Exception],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    repository, protector = _AdmissionPortSpy(events), _ProtectorSpy(events)
    original = admission.fingerprint_analysis_request

    def fingerprint(**kwargs: Any) -> bytes:
        events.append("fingerprint")
        return original(**kwargs)

    monkeypatch.setattr(admission, "fingerprint_analysis_request", fingerprint)
    with pytest.raises(error_type) as caught:
        _admit(repository, protector, **{field: bad})
    _assert_fixed_failure(caught.value, message)
    assert events == []
    assert protector.calls == []
    assert repository.calls == []
    assert caplog.records == []


@pytest.mark.parametrize("field", ("video_id", "player_id", "video_reference"))
@pytest.mark.parametrize("bad", _BAD_TEXT_TYPES)
def test_guard_admission_nonexact_identifier_types_fail_before_boundary(
    field: str, bad: object
) -> None:
    events: list[str] = []
    repository, protector = _AdmissionPortSpy(events), _ProtectorSpy(events)
    with pytest.raises(TypeError) as caught:
        _admit(repository, protector, **{field: bad})
    _assert_fixed_failure(caught.value, "Admission identifiers must be strings.")
    assert events == []


@pytest.mark.parametrize("bad", _BAD_UUIDS)
def test_existing_admission_uuid_rule_is_strict_before_boundary(bad: object) -> None:
    events: list[str] = []
    repository, protector = _AdmissionPortSpy(events), _ProtectorSpy(events)
    with pytest.raises((TypeError, ValueError)):
        _admit(repository, protector, job_id=bad)
    assert events == []


@pytest.mark.parametrize("cls", (HttpUrl, _UrlSubclass))
def test_guard_valid_admission_preserves_order_and_fingerprint(
    cls: type[HttpUrl], monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    repository, protector = _AdmissionPortSpy(events), _ProtectorSpy(events)
    original = admission.fingerprint_analysis_request

    def fingerprint(**kwargs: Any) -> bytes:
        events.append("fingerprint")
        return original(**kwargs)

    monkeypatch.setattr(admission, "fingerprint_analysis_request", fingerprint)
    result = _admit(repository, protector, callback_url=cls("HTTPS://EXAMPLE.COM:443/callback"))
    assert result is repository.result
    assert events == ["protect", "fingerprint", "repository"]
    assert protector.calls == [("apex:tenant-123", "opaque-key")]
    assert len(repository.calls) == 1
    candidate, capacity = repository.calls[0]
    assert capacity == 3
    assert isinstance(candidate, durable.NewAnalysisJob)
    assert candidate.callback_url == _CALLBACK
    assert type(candidate.callback_url) is str
    assert candidate.request_fingerprint == _DIGEST
    assert candidate.idempotency_lookup == b"protected"


@pytest.mark.parametrize("url", (None, _CALLBACK, HttpUrl(_CALLBACK + "#private-fragment")))
def test_guard_admission_callback_failure_precedes_protector_and_fingerprint(
    url: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    repository, protector = _AdmissionPortSpy(events), _ProtectorSpy(events)

    def fingerprint(**kwargs: Any) -> bytes:
        events.append("fingerprint")
        return _DIGEST

    monkeypatch.setattr(admission, "fingerprint_analysis_request", fingerprint)
    with pytest.raises((TypeError, ValueError)):
        _admit(repository, protector, callback_url=url)
    assert events == []


@pytest.mark.parametrize(
    ("url", "hex_digest"),
    (
        (_CALLBACK, _DIGEST.hex()),
        (
            "https://example.com/a%2Fb",
            "6b12df1de7c4e000ed8249ee3d84180c36b5b147e7822bc1dac7295a16394dda",
        ),
        (
            "https://example.com/a%2fb",
            "da28b37627403dd0745b3b88526ef7119997c1b58cddc045e733b4db0979a287",
        ),
        (
            "https://example.com/callback?b=2&a=1",
            "687bc202ab3f0e1cb2745d099d3097bee8c56e48164e69416a1f01bff4c97217",
        ),
    ),
)
def test_guard_fingerprint_v1_fixed_vectors_and_preimage(url: str, hex_digest: str) -> None:
    preimage = json.dumps(
        {
            "fingerprintVersion": "super7.analysis-job-fingerprint.v1",
            "callerScope": "apex:tenant-123",
            "videoId": "video-123",
            "playerId": "player-456",
            "videoReference": "match-01.mp4",
            "callbackUrl": url,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8", errors="strict")
    digest = admission.fingerprint_analysis_request(
        caller_scope="apex:tenant-123",
        video_id="video-123",
        player_id="player-456",
        video_reference="match-01.mp4",
        callback_url=url,
    )
    assert digest == hashlib.sha256(preimage).digest() == bytes.fromhex(hex_digest)
    assert type(digest) is bytes


def test_guard_fingerprint_v1_nonascii_algorithm_vector_remains_unchanged() -> None:
    digest = admission.fingerprint_analysis_request(
        caller_scope="apex:ténant-123",
        video_id="vidéo-123",
        player_id="player-é",
        video_reference="match-é.mp4",
        callback_url="https://example.com/r%C3%A9sultat",
    )
    assert digest.hex() == "abe00c9b17ca68a0e87925cd66a60c11a769354b04c88578e9e40a848da598d7"
    # V1 is unchanged even though RED-F admission rejects these unsafe IDs.


def test_guard_hmac_framing_remains_unchanged() -> None:
    from adapters.hmac_idempotency_lookup_protector import HmacIdempotencyLookupProtector

    scope, key, secret = "apex:tenant-123", "opaque-key-456", bytes(range(32))
    scope_bytes, key_bytes = scope.encode("utf-8"), key.encode("utf-8")
    frame = (
        b"super7:idempotency-lookup:v1\x00"
        + len(scope_bytes).to_bytes(8, "big")
        + scope_bytes
        + len(key_bytes).to_bytes(8, "big")
        + key_bytes
    )
    result = HmacIdempotencyLookupProtector(secret).protect(
        caller_scope=scope, raw_idempotency_key=key
    )
    assert result == hmac.new(secret, frame, hashlib.sha256).digest()
    assert type(result) is bytes


@pytest.mark.parametrize("length", (1, 7, 32, 65))
def test_guard_lookup_boundary_remains_algorithm_independent(length: int) -> None:
    events: list[str] = []
    protector = _ProtectorSpy(events, b"x" * length)
    result = admission.protect_idempotency_lookup(
        protector, caller_scope="scope", raw_idempotency_key="key"
    )
    assert result is protector.output
    assert events == ["protect"]
    candidate = cast(
        durable.NewAnalysisJob, _durable_value("NewAnalysisJob", idempotency_lookup=result)
    )
    assert candidate.idempotency_lookup == result


_TIMESTAMP_FIELDS = (
    ("ClaimableAnalysisJob", "accepted_at"),
    ("WorkerClaim", "started_at"),
    ("WorkerClaim", "heartbeat_at"),
    ("WorkerClaim", "lease_expires_at"),
    ("LeaseRenewed", "heartbeat_at"),
    ("LeaseRenewed", "lease_expires_at"),
)
_BAD_TIMESTAMPS = (
    pytest.param(None, id="none"),
    pytest.param("2026-10-07", id="text"),
    pytest.param(datetime(2026, 10, 7), id="naive"),
    pytest.param(datetime(2026, 10, 7, tzinfo=_NoOffset()), id="no-offset"),
)


@pytest.mark.parametrize(("name", "field"), _TIMESTAMP_FIELDS)
@pytest.mark.parametrize("bad", _BAD_TIMESTAMPS)
def test_timestamps_require_aware_datetime_with_valid_offset(
    name: str, field: str, bad: object
) -> None:
    _symbol(name)
    with pytest.raises((TypeError, ValueError)):
        _value(name, **{field: bad})


@pytest.mark.parametrize("field", ("accepted_at",))
@pytest.mark.parametrize("bad", _BAD_TIMESTAMPS)
def test_existing_job_timestamp_requires_valid_aware_datetime(field: str, bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        _durable_value("AnalysisJob", **{field: bad})


def test_aware_nonutc_timestamps_and_equal_start_heartbeat_are_preserved() -> None:
    local_time = _TIME.astimezone(timezone(timedelta(hours=3)))
    expiry = local_time + timedelta(microseconds=1)
    job = _claim_job(accepted_at=local_time)
    claim = _claim(job=job, started_at=local_time, heartbeat_at=local_time, lease_expires_at=expiry)
    assert job.accepted_at is local_time
    assert claim.started_at is local_time
    assert claim.heartbeat_at is local_time
    assert claim.lease_expires_at is expiry
    renewed = _outcome("LeaseRenewed", heartbeat_at=local_time, lease_expires_at=expiry)
    assert renewed.heartbeat_at is local_time
    assert renewed.lease_expires_at is expiry


@pytest.mark.parametrize(
    ("heartbeat", "expiry"),
    (
        (_TIME - timedelta(microseconds=1), _TIME + timedelta(seconds=1)),
        (_TIME, _TIME),
        (_TIME, _TIME - timedelta(microseconds=1)),
    ),
)
def test_active_claim_timestamp_order_is_exact(heartbeat: datetime, expiry: datetime) -> None:
    _future(_DOMAIN)
    with pytest.raises((TypeError, ValueError)):
        _claim(heartbeat_at=heartbeat, lease_expires_at=expiry)


@pytest.mark.parametrize("expiry", (_TIME, _TIME - timedelta(microseconds=1)))
def test_renewal_expiry_must_be_strictly_after_heartbeat(expiry: datetime) -> None:
    _future(_SERVICE)
    with pytest.raises((TypeError, ValueError)):
        _outcome("LeaseRenewed", lease_expires_at=expiry)


@pytest.mark.parametrize("status_name", ("RETRYABLE_FAILURE", "TERMINAL_FAILURE", "EXHAUSTED"))
def test_resolved_replay_accepts_only_classified_resolution_status(status_name: str) -> None:
    status = getattr(_symbol("AttemptStatus"), status_name)
    value = _outcome("AttemptResolutionAlreadyApplied", status=status)
    assert value.status is status
    assert value.failure_code.value == "retryable"


@pytest.mark.parametrize("status_name", ("ACTIVE", "INTERRUPTED"))
def test_resolved_replay_rejects_active_or_interruption_without_failure_code(
    status_name: str,
) -> None:
    status = getattr(_symbol("AttemptStatus"), status_name)
    with pytest.raises((TypeError, ValueError)):
        _outcome("AttemptResolutionAlreadyApplied", status=status)


@pytest.mark.parametrize(
    "name", ("AttemptFailed", "AttemptExhausted", "AttemptResolutionAlreadyApplied")
)
def test_classified_resolution_requires_typed_nonnull_failure_code(name: str) -> None:
    _symbol(name)
    for invalid in (None, "retryable"):
        with pytest.raises((TypeError, ValueError)):
            _outcome(name, failure_code=invalid)


@pytest.mark.parametrize("field", ("requeued_count", "exhausted_count"))
@pytest.mark.parametrize("bad", (True, -1, 1.0, None, _IntSubclass(0)))
def test_recovery_counts_are_exact_nonnegative_integers(field: str, bad: object) -> None:
    cls = _symbol("RecoverySummary")
    with pytest.raises((TypeError, ValueError)):
        cls(**({"requeued_count": 0, "exhausted_count": 0} | {field: bad}))


def test_recovery_zero_and_nonzero_counts_are_valid() -> None:
    assert _outcome("RecoverySummary").requeued_count == 0
    summary = _outcome("RecoverySummary", requeued_count=2, exhausted_count=3)
    assert (summary.requeued_count, summary.exhausted_count) == (2, 3)


@pytest.mark.parametrize(("operation", "method", "arguments", "outcomes"), _OPERATIONS)
def test_port_and_service_signatures_annotations_and_unions(
    operation: str, method: str, arguments: tuple[str, ...], outcomes: tuple[str, ...]
) -> None:
    module = _future(_SERVICE)
    port = module.AnalysisJobWorkerRepository
    service = getattr(module, operation)
    assert getattr(port, "_is_protocol", False) is True
    assert {
        name for name, value in vars(port).items() if not name.startswith("_") and callable(value)
    } == {row[1] for row in _OPERATIONS}
    expected_arguments = {
        "worker_slot_id": _symbol("WorkerSlotId"),
        "lease_duration": _symbol("LeaseDuration"),
        "authority": _symbol("AttemptAuthority"),
        "failure_code": _symbol("AttemptFailureCode"),
        "batch_size": _symbol("RecoveryBatchSize"),
    }
    for function, first in ((getattr(port, method), "self"), (service, "repository")):
        parameters = inspect.signature(function).parameters
        assert tuple(parameters) == (first, *arguments)
        for name, parameter in parameters.items():
            assert parameter.default is inspect.Parameter.empty
            assert parameter.kind is (
                inspect.Parameter.POSITIONAL_OR_KEYWORD
                if name == "self"
                else inspect.Parameter.KEYWORD_ONLY
            )
        assert inspect.iscoroutinefunction(function)
        hints = get_type_hints(function)
        expected = {name: expected_arguments[name] for name in arguments}
        if first == "repository":
            expected["repository"] = port
        result_type = hints.pop("return")
        assert hints == expected
        approved = tuple(_symbol(name) for name in outcomes)
        if len(approved) == 1:
            assert result_type is approved[0]
        else:
            assert get_origin(result_type) in (UnionType, Union)
            assert set(get_args(result_type)) == set(approved)


@pytest.mark.parametrize("case", ("approved", "rewritten-generation", "plain-enum"))
def test_helper_fencing_and_status_assertions_reject_reviewed_adversaries(
    case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # These values characterize assertions only; they implement no worker policy.
    @dataclass(frozen=True, slots=True)
    class Slot:
        value: UUID

    @dataclass(frozen=True, slots=True)
    class Lease:
        value: UUID

    @dataclass(frozen=True, slots=True)
    class Authority:
        job_id: UUID
        attempt_number: int
        worker_slot_id: Slot
        lease_id: Lease

        def __post_init__(self) -> None:
            if case == "rewritten-generation":
                object.__setattr__(self, "attempt_number", 1)

    vocabulary = (
        "ACTIVE",
        "INTERRUPTED",
        "RETRYABLE_FAILURE",
        "TERMINAL_FAILURE",
        "EXHAUSTED",
        "SUCCEEDED",
    )
    base = cast(Any, Enum if case == "plain-enum" else StrEnum)
    symbols = {
        "AttemptStatus": base("AttemptStatus", {name: name for name in vocabulary}),
        "WorkerSlotId": Slot,
        "LeaseId": Lease,
        "AttemptAuthority": Authority,
    }
    monkeypatch.setattr(sys.modules[__name__], "_symbol", lambda name: symbols[name])
    if case == "approved":
        test_status_vocabulary_and_four_part_authority_are_exact()
    else:
        with pytest.raises(AssertionError):
            test_status_vocabulary_and_four_part_authority_are_exact()


def _annotation_probe(
    monkeypatch: pytest.MonkeyPatch, operation: str, kind: str, side: str
) -> None:
    """Exercise the actual signature assertion with non-executable typed surfaces."""
    symbols = {name: type(name, (), {}) for name in (*_DOMAIN_EXPORTS, *_SERVICE_EXPORTS)}

    class Port(Protocol):
        async def claim_next(self, *, worker_slot_id: object, lease_duration: object) -> object:
            raise AssertionError("Annotation fixture must not execute")

        async def renew_lease(self, *, authority: object, lease_duration: object) -> object:
            raise AssertionError("Annotation fixture must not execute")

        async def finalize_retryable_failure(
            self, *, authority: object, failure_code: object
        ) -> object:
            raise AssertionError("Annotation fixture must not execute")

        async def finalize_terminal_failure(
            self, *, authority: object, failure_code: object
        ) -> object:
            raise AssertionError("Annotation fixture must not execute")

        async def recover_expired(self, *, batch_size: object) -> object:
            raise AssertionError("Annotation fixture must not execute")

    async def claim_next_analysis_job(
        *, repository: object, worker_slot_id: object, lease_duration: object
    ) -> object:
        raise AssertionError("Annotation fixture must not execute")

    async def recover_expired_analysis_jobs(*, repository: object, batch_size: object) -> object:
        raise AssertionError("Annotation fixture must not execute")

    _, method, arguments, outcomes = next(row for row in _OPERATIONS if row[0] == operation)
    argument_types = {
        "worker_slot_id": symbols["WorkerSlotId"],
        "lease_duration": symbols["LeaseDuration"],
        "batch_size": symbols["RecoveryBatchSize"],
    }
    approved = tuple(symbols[name] for name in outcomes)
    expected: Any = approved[0]
    for member in approved[1:]:
        expected |= member
    annotations: dict[str, object] = {
        "approved": expected,
        "typing-union": Union[approved],  # noqa: UP007 - characterize typing.Union compatibility
        "tuple-members": GenericAlias(tuple, approved),
        "tuple-wrapped": tuple[expected],
        "list-wrapped": list[expected],
        "extra-member": expected | object,
        "any": Any,
        "object": object,
    }
    port_method = getattr(Port, method)
    port_method.__annotations__ = {name: argument_types[name] for name in arguments} | {
        "return": annotations[kind] if side == "protocol" else expected
    }
    service = (
        claim_next_analysis_job
        if operation == "claim_next_analysis_job"
        else recover_expired_analysis_jobs
    )
    service.__annotations__ = {name: argument_types[name] for name in arguments} | {
        "repository": Port,
        "return": annotations[kind] if side == "service" else expected,
    }
    module = ModuleType("reviewed_annotation_fixture")
    module.__dict__["AnalysisJobWorkerRepository"] = Port
    setattr(module, operation, service)
    monkeypatch.setattr(sys.modules[__name__], "_symbol", lambda name: symbols[name])
    monkeypatch.setattr(sys.modules[__name__], "_future", lambda name: module)
    test_port_and_service_signatures_annotations_and_unions(operation, method, arguments, outcomes)


@pytest.mark.parametrize(
    ("operation", "kind", "side"),
    (
        ("recover_expired_analysis_jobs", "approved", "protocol"),
        ("claim_next_analysis_job", "approved", "protocol"),
        ("claim_next_analysis_job", "typing-union", "protocol"),
        ("claim_next_analysis_job", "typing-union", "service"),
    ),
)
def test_helper_return_annotations_accept_exact_class_and_equivalent_unions(
    operation: str, kind: str, side: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _annotation_probe(monkeypatch, operation, kind, side)


@pytest.mark.parametrize("side", ("protocol", "service"))
@pytest.mark.parametrize(
    ("operation", "kind"),
    tuple(
        (operation, kind)
        for operation in ("recover_expired_analysis_jobs", "claim_next_analysis_job")
        for kind in (
            "tuple-members",
            "tuple-wrapped",
            "list-wrapped",
            "extra-member",
            "any",
            "object",
        )
        if not (operation == "recover_expired_analysis_jobs" and kind == "tuple-wrapped")
    ),
)
def test_helper_return_annotations_reject_wrappers_extra_members_and_loose_types(
    operation: str, kind: str, side: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(AssertionError):
        _annotation_probe(monkeypatch, operation, kind, side)


_DELEGATIONS = tuple(
    (operation, method, arguments, outcome)
    for operation, method, arguments, outcomes in _OPERATIONS
    for outcome in outcomes
)


@pytest.mark.parametrize(("operation", "method", "arguments", "outcome_name"), _DELEGATIONS)
def test_services_delegate_once_and_preserve_every_approved_outcome(
    operation: str,
    method: str,
    arguments: tuple[str, ...],
    outcome_name: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = getattr(_future(_SERVICE), operation)
    result = _claim() if outcome_name == "WorkerClaim" else _outcome(outcome_name)
    spy = _WorkerPortSpy(result)
    supplied = _operation_arguments(arguments)
    observed = asyncio.run(service(repository=spy, **supplied))
    assert observed is result
    assert len(spy.calls) == 1
    called_method, called_arguments = spy.calls[0]
    assert called_method == method
    assert tuple(called_arguments) == arguments
    assert all(called_arguments[name] is supplied[name] for name in arguments)
    for record in caplog.records:
        assert record.exc_info is None
        assert not any(
            marker in record.getMessage() for marker in (_CALLBACK, "match-01.mp4", str(_LEASE))
        )


@pytest.mark.parametrize(("operation", "method", "arguments", "outcomes"), _OPERATIONS)
@pytest.mark.parametrize("bad", (None, 1, "raw", object()))
def test_services_reject_wrong_public_input_types_before_port_io(
    operation: str, method: str, arguments: tuple[str, ...], outcomes: tuple[str, ...], bad: object
) -> None:
    service = getattr(_future(_SERVICE), operation)
    for name in arguments:
        supplied = _operation_arguments(arguments)
        supplied[name] = bad
        spy = _WorkerPortSpy()
        with pytest.raises((TypeError, ValueError)):
            asyncio.run(service(repository=spy, **supplied))
        assert spy.calls == []


@pytest.mark.parametrize(("operation", "method", "arguments", "outcomes"), _OPERATIONS)
@pytest.mark.parametrize(
    "kind",
    (
        "runtime",
        "attribute",
        "type",
        "value",
        "cancelled",
        "keyboard",
        "exit",
        "memory",
        "fatal",
        "unavailable",
        "invariant",
    ),
)
def test_service_exceptions_propagate_by_identity_without_retry_or_resolution(
    operation: str, method: str, arguments: tuple[str, ...], outcomes: tuple[str, ...], kind: str
) -> None:
    module = _future(_SERVICE)
    errors: dict[str, BaseException] = {
        "runtime": RuntimeError("unexpected"),
        "attribute": AttributeError("unexpected"),
        "type": TypeError("unexpected"),
        "value": ValueError("unexpected"),
        "cancelled": asyncio.CancelledError(),
        "keyboard": KeyboardInterrupt(),
        "exit": SystemExit(),
        "memory": MemoryError(),
        "fatal": _FatalFailure(),
        "unavailable": module.AnalysisJobWorkerPersistenceUnavailable(),
        "invariant": module.AnalysisJobWorkerPersistenceInvariantError(),
    }
    error = errors[kind]
    supplied = _operation_arguments(arguments)
    spy = _WorkerPortSpy(error=error)
    with pytest.raises(type(error)) as caught:
        asyncio.run(getattr(module, operation)(repository=spy, **supplied))
    assert caught.value is error
    assert spy.calls == [(method, supplied)]


def test_public_persistence_errors_are_distinct_runtime_errors() -> None:
    module = _future(_SERVICE)
    unavailable = module.AnalysisJobWorkerPersistenceUnavailable
    invariant = module.AnalysisJobWorkerPersistenceInvariantError
    assert unavailable is not invariant
    assert unavailable.__bases__ == (RuntimeError,)
    assert invariant.__bases__ == (RuntimeError,)


def test_claim_slot_unavailable_has_no_mutation_authority() -> None:
    value = _outcome("WorkerSlotUnavailable")
    assert tuple(item.name for item in fields(value)) == ("worker_slot_id",)
    for name in ("authority", "lease_id", "job_id", "attempt_number"):
        assert not hasattr(value, name)
    lost = _outcome("ClaimLost")
    assert tuple(item.name for item in fields(lost)) == ("authority",)


def test_future_services_have_no_success_cancellation_execution_or_io_surface() -> None:
    module = _future(_SERVICE)
    forbidden = {
        "finalize_success",
        "finalize_successful_analysis",
        "cancel_analysis_job",
        "run_worker",
        "worker_loop",
        "dispatch_callbacks",
    }
    assert not forbidden.intersection(vars(module))
    tree = ast.parse(inspect.getsource(module))
    imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name.split(".")[0] in {"psycopg", "psycopg_pool", "adapters", "main", "fastapi"}
        for name in imports
    )


def test_guard_application_configuration_and_routes_remain_inactive() -> None:
    import main
    from config.analysis import DEFAULT_MAX_ACTIVE_ANALYSES
    from core.config import Settings
    from services.analysis_queue import AnalysisQueue, AnalysisWorker

    assert set(main.create_app.__annotations__).isdisjoint(
        {"worker_repository", "analysis_job_repository", "idempotency_lookup_protector"}
    )
    assert set(inspect.signature(main.create_app).parameters) == {
        "settings",
        "tracker",
        "selector",
        "validator",
        "lifecycle",
        "downloader",
        "path_resolver",
        "callback_service",
        "analysis_queue",
        "process_analysis_pool",
        "durable_runtime",
    }
    assert isinstance(main.app.state.analysis_queue, AnalysisQueue)
    assert isinstance(main.app.state.analysis_worker, AnalysisWorker)
    assert (
        main.app.state.analysis_queue.metrics().max_active_analyses
        == DEFAULT_MAX_ACTIVE_ANALYSES
        == 1
    )
    assert not main.app.state.analysis_queue.metrics().worker_running
    assert set(Settings.__dataclass_fields__).isdisjoint(
        {
            "attempt_limit",
            "lease_duration",
            "heartbeat_cadence",
            "recovery_batch_size",
            "worker_slot_id",
        }
    )
    assert set(main.app.openapi()["paths"]) == {
        "/health/live",
        "/health/ready",
        "/health",
        "/analyze",
    }


def test_guard_runtime_sources_do_not_import_worker_persistence() -> None:
    forbidden = {
        _DOMAIN,
        _SERVICE,
        "adapters.psycopg_analysis_job_worker_repository",
        "adapters.hmac_idempotency_lookup_protector",
    }
    for relative in (
        "src/main.py",
        "src/api/routes.py",
        "src/core/config.py",
        "src/services/analysis_queue.py",
        "src/services/process_analysis_pool.py",
    ):
        tree = ast.parse((_ROOT / relative).read_text(encoding="utf-8"))
        imports = {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        imports.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not imports.intersection(forbidden)


class _FoldTimezone(tzinfo):
    """Local fold fixture, so ordering coverage never needs a timezone download."""

    def utcoffset(self, dt: datetime | None) -> timedelta:
        return timedelta(hours=0 if dt is not None and dt.fold else 1)

    def dst(self, dt: datetime | None) -> timedelta:
        return timedelta(0)

    def tzname(self, dt: datetime | None) -> str:
        return "local-fold"


def _fold_timezone(local: bool) -> tzinfo:
    if not local:
        try:
            return ZoneInfo("Europe/London")
        except ZoneInfoNotFoundError:
            pass
    return _FoldTimezone()


@pytest.mark.parametrize("local", (False, True), ids=("london-or-local", "local"))
@pytest.mark.parametrize("ordering", ("valid", "reversed", "equal"))
@pytest.mark.parametrize("boundary", ("claim-heartbeat", "claim-expiry", "renewal-expiry"))
def test_absolute_timestamp_order_across_folds(local: bool, ordering: str, boundary: str) -> None:
    zone = _fold_timezone(local)
    later = datetime(2026, 10, 25, 1, 30, tzinfo=zone, fold=1)
    earlier = datetime(2026, 10, 25, 1, 45, tzinfo=zone, fold=0)
    assert earlier.astimezone(UTC) < later.astimezone(UTC)
    assert earlier.replace(tzinfo=None) > later.replace(tzinfo=None)
    if ordering == "valid":
        left, right = earlier, later
    elif ordering == "reversed":
        left, right = later, earlier
    else:
        left, right = later, later.astimezone(UTC)
        assert left.astimezone(UTC) == right

    if boundary == "claim-heartbeat":
        name = "WorkerClaim"
        supplied = dict(
            started_at=left, heartbeat_at=right, lease_expires_at=right + timedelta(hours=3)
        )
    elif boundary == "claim-expiry":
        name = "WorkerClaim"
        supplied = dict(
            started_at=datetime(2026, 10, 25, tzinfo=UTC),
            heartbeat_at=left,
            lease_expires_at=right,
        )
    else:
        name = "LeaseRenewed"
        supplied = dict(heartbeat_at=left, lease_expires_at=right)

    accepted = ordering == "valid" or (ordering == "equal" and boundary == "claim-heartbeat")
    if not accepted:
        with pytest.raises(ValueError):
            _value(name, **supplied)
        return
    value = _value(name, **supplied)
    for field, timestamp in supplied.items():
        assert getattr(value, field) is timestamp
        assert getattr(value, field).utcoffset() == timestamp.utcoffset()
        assert getattr(value, field).fold == timestamp.fold


@pytest.mark.parametrize("name", ("WorkerClaim", "LeaseRenewed"))
@pytest.mark.parametrize("upper", (False, True), ids=("minimum", "maximum"))
def test_absolute_timestamp_order_preserves_extreme_aware_values(name: str, upper: bool) -> None:
    if upper:
        right = datetime.max.replace(tzinfo=timezone(timedelta(hours=-23)))
        left = right - timedelta(microseconds=1)
    else:
        left = datetime.min.replace(tzinfo=timezone(timedelta(hours=23)))
        right = left + timedelta(microseconds=1)
    with pytest.raises(OverflowError):
        left.astimezone(UTC)
    supplied = dict(heartbeat_at=left, lease_expires_at=right)
    if name == "WorkerClaim":
        supplied["started_at"] = left
    value = _value(name, **supplied)
    for field, timestamp in supplied.items():
        assert getattr(value, field) is timestamp
        assert getattr(value, field).utcoffset() == timestamp.utcoffset()
