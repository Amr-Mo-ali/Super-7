"""RED-A contract for inactive durable AnalysisJob admission."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from importlib.util import find_spec
from types import ModuleType
from typing import Any, cast, get_args, get_type_hints
from uuid import UUID

import pytest
from pydantic import ValidationError

from schemas.analysis import AnalyzeRequest
from services.video_path_resolver import VideoPathResolver

_DOMAIN_MODULE = "domain.analysis_job"
_ADMISSION_MODULE = "services.analysis_job_admission"
_FINGERPRINT_TAG = "super7.analysis-job-fingerprint.v1"
_BASELINE_DIGEST = bytes.fromhex("b99dcef54edc40c22b91871a4c6838aa46abe5069b0abcc814d2188a4427b77b")
_NON_ASCII_DIGEST = bytes.fromhex(
    "abe00c9b17ca68a0e87925cd66a60c11a769354b04c88578e9e40a848da598d7"
)
_RAW_KEY_MARKER = "raw-key-red-a-marker-never-render"
_JOB_ID = UUID("12345678-1234-5678-1234-567812345678")
_ACCEPTED_AT = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_MAX_SCAN_DEPTH = 16
_MAX_SCAN_NODES = 256
_MAX_SCAN_VALUE_LENGTH = 65_536
_EXCEPTION_ARGS_DESCRIPTOR = cast(Any, BaseException.__dict__["args"])
_EXCEPTION_DICT_DESCRIPTOR = cast(Any, BaseException.__dict__["__dict__"])
_EXCEPTION_CAUSE_DESCRIPTOR = cast(Any, BaseException.__dict__["__cause__"])
_EXCEPTION_CONTEXT_DESCRIPTOR = cast(Any, BaseException.__dict__["__context__"])
_EXCEPTION_GROUP_MEMBERS_DESCRIPTOR = cast(Any, BaseExceptionGroup.__dict__["exceptions"])


def _require_module(module_name: str, purpose: str) -> ModuleType:
    if find_spec(module_name) is None:
        pytest.fail(
            f"Slice 2 RED-A: {module_name} is missing; required for {purpose}.",
            pytrace=False,
        )
    return import_module(module_name)


def _require_symbol(module_name: str, symbol: str, purpose: str) -> Any:
    module = _require_module(module_name, purpose)
    if not hasattr(module, symbol):
        pytest.fail(
            f"Slice 2 RED-A: {module_name}.{symbol} is missing; required for {purpose}.",
            pytrace=False,
        )
    return getattr(module, symbol)


def _request(callback_url: str = "https://example.com/callback") -> AnalyzeRequest:
    return AnalyzeRequest.model_validate(
        {
            "videoId": "video-123",
            "playerId": "player-456",
            "videoUrl": "match-01.mp4",
            "callbackUrl": callback_url,
        }
    )


def _canonical_callback_url(value: str) -> str:
    canonicalize = _require_symbol(
        _ADMISSION_MODULE,
        "canonicalize_callback_url",
        "canonical validated callback URLs and fragment rejection",
    )
    return str(canonicalize(_request(value).callback_url))


def _fingerprint(
    *,
    caller_scope: str = "apex:tenant-123",
    video_id: str = "video-123",
    player_id: str = "player-456",
    video_reference: str = "match-01.mp4",
    callback_url: str = "https://example.com/callback",
) -> bytes:
    fingerprint = _require_symbol(
        _ADMISSION_MODULE,
        "fingerprint_analysis_request",
        "the frozen SHA-256 request fingerprint",
    )
    return cast(
        bytes,
        fingerprint(
            caller_scope=caller_scope,
            video_id=video_id,
            player_id=player_id,
            video_reference=video_reference,
            callback_url=callback_url,
        ),
    )


def _reference_fingerprint(**overrides: str) -> bytes:
    values = {
        "fingerprintVersion": _FINGERPRINT_TAG,
        "callerScope": "apex:tenant-123",
        "videoId": "video-123",
        "playerId": "player-456",
        "videoReference": "match-01.mp4",
        "callbackUrl": "https://example.com/callback",
    }
    values.update(overrides)
    preimage = json.dumps(
        values,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    ).encode("utf-8", errors="strict")
    return hashlib.sha256(preimage).digest()


def _job() -> Any:
    state_type = _require_symbol(_DOMAIN_MODULE, "AnalysisJobState", "durable job states")
    job_type = _require_symbol(_DOMAIN_MODULE, "AnalysisJob", "the immutable durable job value")
    return job_type(
        job_id=_JOB_ID,
        video_id="video-123",
        player_id="player-456",
        video_reference="match-01.mp4",
        callback_url="https://example.com/callback",
        state=state_type.QUEUED,
        accepted_at=_ACCEPTED_AT,
    )


def _candidate(
    *,
    idempotency_lookup: bytes = b"\x11" * 32,
    request_fingerprint: bytes = _BASELINE_DIGEST,
) -> Any:
    candidate_type = _require_symbol(
        _DOMAIN_MODULE, "NewAnalysisJob", "the protected immutable admission candidate"
    )
    return candidate_type(
        job_id=_JOB_ID,
        idempotency_lookup=idempotency_lookup,
        request_fingerprint=request_fingerprint,
        video_id="video-123",
        player_id="player-456",
        video_reference="match-01.mp4",
        callback_url="https://example.com/callback",
    )


def _sensitive_representations(
    sensitive_value: str,
) -> tuple[tuple[str, ...], tuple[bytes, ...]]:
    if type(sensitive_value) is not str or not sensitive_value:
        raise AssertionError("sensitive value leaked")
    text_representations = (
        sensitive_value,
        repr(sensitive_value),
        ascii(sensitive_value),
        sensitive_value.encode("unicode_escape").decode("ascii"),
    )
    byte_representations = tuple(
        item.encode("utf-8", errors="backslashreplace") for item in text_representations
    )
    return text_representations, byte_representations


def _is_supported_and_sensitive_free(sensitive_value: str, *values: object) -> bool:
    text_representations, byte_representations = _sensitive_representations(sensitive_value)
    pending = [(value, 0) for value in values]
    seen_containers: set[int] = set()
    visited_nodes = 0

    while pending:
        value, depth = pending.pop()
        value_type = type(value)
        is_container = value_type in (tuple, list, dict, set, frozenset)
        if is_container:
            identity = id(value)
            if identity in seen_containers:
                continue
            seen_containers.add(identity)
        if depth > _MAX_SCAN_DEPTH:
            return False
        visited_nodes += 1
        if visited_nodes > _MAX_SCAN_NODES:
            return False

        if value_type is str:
            text_value = cast(str, value)
            if len(text_value) > _MAX_SCAN_VALUE_LENGTH:
                return False
            if any(item in text_value for item in text_representations):
                return False
            continue
        if value_type in (bytes, bytearray):
            binary_value = cast(bytes | bytearray, value)
            if len(binary_value) > _MAX_SCAN_VALUE_LENGTH:
                return False
            if any(item in binary_value for item in byte_representations):
                return False
            continue
        if value_type is memoryview:
            memory_value = cast(memoryview, value)
            if memory_value.nbytes > _MAX_SCAN_VALUE_LENGTH:
                return False
            raw_value = memory_value.tobytes()
            if any(item in raw_value for item in byte_representations):
                return False
            continue
        if value is None or value_type in (bool, int, float):
            continue
        if value_type in (tuple, list, set, frozenset):
            sequence_value = cast(
                tuple[object, ...] | list[object] | set[object] | frozenset[object], value
            )
            if len(sequence_value) > _MAX_SCAN_NODES:
                return False
            pending.extend((item, depth + 1) for item in sequence_value)
            continue
        if value_type is dict:
            mapping_value = cast(dict[object, object], value)
            if len(mapping_value) * 2 > _MAX_SCAN_NODES:
                return False
            for key, item in mapping_value.items():
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
            continue
        return False

    return True


def _assert_sensitive_value_absent(sensitive_value: str, *values: object) -> None:
    if not _is_supported_and_sensitive_free(sensitive_value, *values):
        raise AssertionError("sensitive value leaked")


def _assert_marker_absent(*values: object) -> None:
    _assert_sensitive_value_absent(_RAW_KEY_MARKER, *values)


def _is_safe_builtin_log_format(message: object, arguments: object) -> bool:
    if type(message) is not str or type(arguments) is not tuple:
        return False
    if not arguments:
        return True
    if any(type(argument) is not str for argument in arguments):
        return False

    placeholders = 0
    index = 0
    while index < len(message):
        if message[index] != "%":
            index += 1
            continue
        if index + 1 >= len(message):
            return False
        directive = message[index + 1]
        if directive == "%":
            index += 2
            continue
        if directive != "s":
            return False
        placeholders += 1
        index += 2
    return placeholders == len(arguments)


def _assert_log_records_are_safe(records: list[logging.LogRecord]) -> None:
    if type(records) is not list:
        raise AssertionError("sensitive value leaked")
    for record in records:
        if type(record) is not logging.LogRecord:
            raise AssertionError("sensitive value leaked")
        _assert_marker_absent(record.msg, record.args, record.__dict__)
        if not _is_safe_builtin_log_format(record.msg, record.args):
            raise AssertionError("sensitive value leaked")
        try:
            rendered_message = logging.LogRecord.getMessage(record)
        except (TypeError, ValueError, OverflowError):
            rendering_failed = True
        else:
            rendering_failed = False
        if rendering_failed:
            raise AssertionError("sensitive value leaked")
        if type(rendered_message) is not str:
            raise AssertionError("sensitive value leaked")
        _assert_marker_absent(rendered_message)


def _assert_exception_graph_is_safe(
    error: BaseException, *, sensitive_value: str = _RAW_KEY_MARKER
) -> None:
    pending = [(error, 0)]
    seen: set[int] = set()
    visited_nodes = 0
    while pending:
        current, depth = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if depth > _MAX_SCAN_DEPTH:
            raise AssertionError("sensitive value leaked")
        visited_nodes += 1
        if visited_nodes > _MAX_SCAN_NODES:
            raise AssertionError("sensitive value leaked")

        arguments = _EXCEPTION_ARGS_DESCRIPTOR.__get__(current, type(current))
        attributes = _EXCEPTION_DICT_DESCRIPTOR.__get__(current, type(current))
        _assert_sensitive_value_absent(sensitive_value, arguments, attributes)

        cause = _EXCEPTION_CAUSE_DESCRIPTOR.__get__(current, type(current))
        context = _EXCEPTION_CONTEXT_DESCRIPTOR.__get__(current, type(current))
        if cause is not None:
            pending.append((cause, depth + 1))
        if context is not None:
            pending.append((context, depth + 1))
        if isinstance(current, BaseExceptionGroup):
            members = _EXCEPTION_GROUP_MEMBERS_DESCRIPTOR.__get__(current, type(current))
            pending.extend((item, depth + 1) for item in members)


def _capture_sensitive_failure(action: Callable[..., None], *arguments: object) -> AssertionError:
    captured: AssertionError | None = None
    try:
        action(*arguments)
    except AssertionError as error:
        captured = error
    if captured is None:
        raise AssertionError("sensitive scanner did not fail closed")
    return captured


def _assert_generic_sensitive_failure(error: AssertionError) -> None:
    assert type(error) is AssertionError
    assert error.args == ("sensitive value leaked",)
    _assert_exception_graph_is_safe(error)


def _assert_exception_graph_with_sensitive_value(
    error: BaseException, sensitive_value: str
) -> None:
    _assert_exception_graph_is_safe(error, sensitive_value=sensitive_value)


def _assert_forbidden_public_fields_absent(value: object, forbidden_fields: set[str]) -> None:
    if any(hasattr(value, field) for field in forbidden_fields):
        raise AssertionError("unexpected public field")


@dataclass(frozen=True, slots=True)
class _FrozenSlottedValue:
    public_value: str


class _HostileRenderingProbe:
    def __init__(self) -> None:
        self.str_calls = 0
        self.repr_calls = 0

    def __str__(self) -> str:
        self.str_calls += 1
        raise RuntimeError("test-owned str hook must not run")

    def __repr__(self) -> str:
        self.repr_calls += 1
        raise RuntimeError("test-owned repr hook must not run")


class _DeterministicProtector:
    def __init__(self) -> None:
        self.calls = 0

    def protect(self, *, caller_scope: str, raw_idempotency_key: str) -> bytes:
        self.calls += 1
        if caller_scope == "scope-a" and raw_idempotency_key == _RAW_KEY_MARKER:
            return b"\x11" * 32
        if caller_scope == "scope-b" and raw_idempotency_key == _RAW_KEY_MARKER:
            return b"\x22" * 32
        raise LookupError("unknown deterministic protector fixture")

    def __repr__(self) -> str:
        return "<deterministic-protector>"


class _FailingProtector:
    def __init__(self, events: list[str] | None = None) -> None:
        self._events = events
        self.calls = 0

    def protect(self, *, caller_scope: str, raw_idempotency_key: str) -> bytes:
        del caller_scope
        self.calls += 1
        if self._events is not None:
            self._events.append("protect")
        failure_type = _require_symbol(
            _ADMISSION_MODULE,
            "IdempotencyLookupProtectionFailure",
            "the narrow expected protector failure taxonomy",
        )
        raise failure_type(raw_idempotency_key)


class _ReturningProtector:
    def __init__(self, returned_value: object, events: list[str] | None = None) -> None:
        self._returned_value = returned_value
        self._events = events
        self.calls = 0

    def protect(self, *, caller_scope: str, raw_idempotency_key: str) -> bytes:
        if type(caller_scope) is not str or type(raw_idempotency_key) is not str:
            raise AssertionError("unexpected protector input")
        self.calls += 1
        if self._events is not None:
            self._events.append("protect")
        return cast(bytes, self._returned_value)


class _RaisingProtector:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    def protect(self, *, caller_scope: str, raw_idempotency_key: str) -> bytes:
        del caller_scope, raw_idempotency_key
        self.calls += 1
        raise self._error


class _RepositorySpy:
    def __init__(self, events: list[str] | None = None) -> None:
        self.calls = 0
        rejected_type = _require_symbol(
            _DOMAIN_MODULE,
            "AnalysisJobCapacityRejected",
            "an approved repository outcome fixture",
        )
        self.outcome = rejected_type()
        self.events = events
        self.candidates: list[object] = []
        self.max_queue_sizes: list[int] = []
        self.protected_lookups: list[bytes] = []

    async def accept_or_get(self, candidate: object, *, max_queue_size: int) -> object:
        protected_lookup = cast(Any, candidate).idempotency_lookup
        if type(protected_lookup) is not bytes:
            raise TypeError("repository candidate requires protected lookup bytes")
        if self.events is not None:
            self.events.append("repository")
        self.calls += 1
        self.candidates.append(candidate)
        self.max_queue_sizes.append(max_queue_size)
        self.protected_lookups.append(protected_lookup)
        return self.outcome


class _BytesSubclass(bytes):
    pass


def _run_admission(
    repository: _RepositorySpy,
    protector: object,
    *,
    caller_scope: object = "apex:tenant-123",
    raw_idempotency_key: object = _RAW_KEY_MARKER,
    video_id: object = "video-123",
    player_id: object = "player-456",
    video_reference: object = "match-01.mp4",
    callback_url: object | None = None,
    max_queue_size: object = 1,
) -> object:
    admit = _require_symbol(
        _ADMISSION_MODULE,
        "admit_analysis_job",
        "the complete bounded admission orchestration",
    )
    resolved_callback = _request().callback_url if callback_url is None else callback_url
    return asyncio.run(
        admit(
            repository=repository,
            protector=protector,
            job_id=_JOB_ID,
            caller_scope=caller_scope,
            raw_idempotency_key=raw_idempotency_key,
            video_id=video_id,
            player_id=player_id,
            video_reference=video_reference,
            callback_url=resolved_callback,
            max_queue_size=max_queue_size,
        )
    )


def _install_order_spies(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
) -> None:
    admission_module = _require_module(
        _ADMISSION_MODULE,
        "ordered fingerprint and candidate construction evidence",
    )
    actual_fingerprint = admission_module.fingerprint_analysis_request
    actual_candidate = admission_module.NewAnalysisJob

    def recording_fingerprint(**arguments: Any) -> bytes:
        events.append("fingerprint")
        return cast(bytes, actual_fingerprint(**arguments))

    def recording_candidate(**arguments: Any) -> object:
        events.append("candidate")
        return actual_candidate(**arguments)

    monkeypatch.setattr(admission_module, "fingerprint_analysis_request", recording_fingerprint)
    monkeypatch.setattr(admission_module, "NewAnalysisJob", recording_candidate)


def test_current_request_contract_remains_runtime_inactive() -> None:
    main = import_module("main")
    parameters = inspect.signature(main.create_app).parameters

    assert set(AnalyzeRequest.model_fields) == {
        "video_id",
        "player_id",
        "video_url",
        "callback_url",
    }
    assert "idempotency_key" not in AnalyzeRequest.model_fields
    assert "analysis_job_repository" not in parameters
    assert "idempotency_lookup_protector" not in parameters


@pytest.mark.parametrize(
    ("supplied", "canonical"),
    (
        ("HTTPS://EXAMPLE.COM/callback", "https://example.com/callback"),
        ("https://example.com:443/callback", "https://example.com/callback"),
        ("https://example.com", "https://example.com/"),
    ),
    ids=("scheme-and-host-case", "default-https-port", "absent-path"),
)
def test_current_http_url_contract_has_stable_canonical_strings(
    supplied: str, canonical: str
) -> None:
    assert str(_request(supplied).callback_url) == canonical


@pytest.mark.parametrize(
    "payload",
    (
        {
            "videoId": "video-123",
            "playerId": "player-456",
            "videoUrl": "match-01.mp4",
        },
        {
            "videoId": "video-123",
            "playerId": "player-456",
            "videoUrl": "match-01.mp4",
            "callbackUrl": "not-a-url",
        },
    ),
    ids=("absent-callback", "invalid-callback"),
)
def test_current_request_rejects_absent_or_invalid_callback(payload: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        AnalyzeRequest.model_validate(payload)


def test_current_video_reference_validation_preserves_the_supplied_value() -> None:
    reference = "Match-é.MP4"
    request = AnalyzeRequest.model_validate(
        {
            "videoId": "video-123",
            "playerId": "player-456",
            "videoUrl": reference,
            "callbackUrl": "https://example.com/callback",
        }
    )
    accepted_reference = request.video_url
    VideoPathResolver("unused-in-lightweight-validation").validate_reference(accepted_reference)

    assert request.video_url == reference
    assert accepted_reference == reference


def test_analysis_job_state_vocabulary_and_lifecycle_are_exact() -> None:
    state_type = _require_symbol(_DOMAIN_MODULE, "AnalysisJobState", "durable job states")
    is_allowed = _require_symbol(
        _DOMAIN_MODULE,
        "is_analysis_job_transition_allowed",
        "the Slice 2 lifecycle graph",
    )
    expected_states = {"QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"}
    allowed = {
        ("QUEUED", "RUNNING"),
        ("QUEUED", "CANCELLED"),
        ("RUNNING", "COMPLETED"),
        ("RUNNING", "FAILED"),
        ("RUNNING", "CANCELLED"),
    }

    assert {member.name for member in state_type} == expected_states
    assert {member.value for member in state_type} == expected_states
    observed = {
        (source.name, target.name)
        for source in state_type
        for target in state_type
        if is_allowed(source, target)
    }
    assert observed == allowed
    assert not is_allowed(state_type.QUEUED, state_type.FAILED)
    assert not is_allowed(state_type.RUNNING, state_type.QUEUED)


def test_baseline_fingerprint_freezes_json_order_serialization_and_binary_digest() -> None:
    expected_preimage = (
        b'{"fingerprintVersion":"super7.analysis-job-fingerprint.v1",'
        b'"callerScope":"apex:tenant-123","videoId":"video-123",'
        b'"playerId":"player-456","videoReference":"match-01.mp4",'
        b'"callbackUrl":"https://example.com/callback"}'
    )
    actual = _fingerprint()

    assert _reference_fingerprint() == hashlib.sha256(expected_preimage).digest()
    assert actual == _BASELINE_DIGEST
    assert type(actual) is bytes
    assert len(actual) == 32


@pytest.mark.parametrize(
    "supplied",
    ("HTTPS://EXAMPLE.COM/callback", "https://example.com:443/callback"),
    ids=("scheme-and-host-case", "default-https-port"),
)
def test_equivalent_callback_urls_have_the_baseline_fingerprint(supplied: str) -> None:
    assert _fingerprint(callback_url=_canonical_callback_url(supplied)) == _BASELINE_DIGEST


@pytest.mark.parametrize(
    ("callback_url", "expected_hex"),
    (
        (
            "https://example.com/callback/",
            "a4156d10cc69da76664e744527dcc7b35844a3724ae203263cfa200ee4c8d590",
        ),
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
        (
            "https://example.com/callback?a=1&b=2",
            "65e76506164f26d39597d5b8f2840165edff7b474c511ac0777de9a5817a0663",
        ),
    ),
    ids=("trailing-slash", "percent-upper", "percent-lower", "query-ba", "query-ab"),
)
def test_distinct_canonical_callback_spellings_have_fixed_fingerprints(
    callback_url: str, expected_hex: str
) -> None:
    canonical = _canonical_callback_url(callback_url)
    assert canonical == callback_url
    assert _fingerprint(callback_url=canonical) == bytes.fromhex(expected_hex)


def test_absent_path_is_canonicalized_before_fingerprinting() -> None:
    canonical = _canonical_callback_url("https://example.com")

    assert canonical == "https://example.com/"
    assert _fingerprint(callback_url=canonical) == _reference_fingerprint(
        callbackUrl="https://example.com/"
    )
    assert _fingerprint(callback_url=canonical) != _BASELINE_DIGEST


def test_callback_fragment_is_rejected_before_fingerprinting() -> None:
    canonicalize = _require_symbol(
        _ADMISSION_MODULE, "canonicalize_callback_url", "callback fragment rejection"
    )
    callback_url = _request("https://example.com/callback#fragment").callback_url

    with pytest.raises(ValueError):
        canonicalize(callback_url)


def test_non_ascii_vector_hashes_the_canonical_http_url_string() -> None:
    supplied_url = "https://example.com/résultat"
    canonical_url = _canonical_callback_url(supplied_url)
    expected_preimage = (
        '{"fingerprintVersion":"super7.analysis-job-fingerprint.v1",'
        '"callerScope":"apex:ténant-123","videoId":"vidéo-123",'
        '"playerId":"player-é","videoReference":"match-é.mp4",'
        '"callbackUrl":"https://example.com/r%C3%A9sultat"}'
    ).encode("utf-8", errors="strict")

    assert canonical_url == "https://example.com/r%C3%A9sultat"
    assert hashlib.sha256(expected_preimage).digest() == _NON_ASCII_DIGEST
    assert (
        _fingerprint(
            caller_scope="apex:ténant-123",
            video_id="vidéo-123",
            player_id="player-é",
            video_reference="match-é.mp4",
            callback_url=canonical_url,
        )
        == _NON_ASCII_DIGEST
    )
    assert (
        _reference_fingerprint(
            callerScope="apex:ténant-123",
            videoId="vidéo-123",
            playerId="player-é",
            videoReference="match-é.mp4",
            callbackUrl=supplied_url,
        )
        != _NON_ASCII_DIGEST
    )


def test_fingerprint_uses_strict_utf8_and_rejects_null_input() -> None:
    rejected_input = "invalid-surrogate-\ud800"
    captured: ValueError | None = None
    try:
        _fingerprint(caller_scope=rejected_input)
    except ValueError as error:
        captured = error
    if captured is None:
        pytest.fail("Invalid Unicode input was not rejected.", pytrace=False)

    _assert_exception_graph_is_safe(captured, sensitive_value=rejected_input)
    with pytest.raises((TypeError, ValueError)):
        _fingerprint(caller_scope=None)  # type: ignore[arg-type]


def test_idempotency_lookup_protector_is_a_minimal_protocol() -> None:
    protector_type = _require_symbol(
        _ADMISSION_MODULE,
        "IdempotencyLookupProtector",
        "the raw-key protection port",
    )
    public_methods = {
        name
        for name, value in vars(protector_type).items()
        if not name.startswith("_") and callable(value)
    }
    parameters = inspect.signature(protector_type.protect).parameters

    assert getattr(protector_type, "_is_protocol", False) is True
    assert public_methods == {"protect"}
    assert tuple(parameters) == ("self", "caller_scope", "raw_idempotency_key")
    assert parameters["caller_scope"].kind is inspect.Parameter.KEYWORD_ONLY
    assert parameters["raw_idempotency_key"].kind is inspect.Parameter.KEYWORD_ONLY


def test_fake_protector_is_scope_aware_and_raw_key_remains_at_the_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    protect = _require_symbol(
        _ADMISSION_MODULE,
        "protect_idempotency_lookup",
        "safe conversion of a raw key to protected bytes",
    )
    fake = _DeterministicProtector()

    first = protect(fake, caller_scope="scope-a", raw_idempotency_key=_RAW_KEY_MARKER)
    second = protect(fake, caller_scope="scope-b", raw_idempotency_key=_RAW_KEY_MARKER)

    _assert_marker_absent(first, second)
    _assert_log_records_are_safe(caplog.records)
    _assert_marker_absent(caplog.text)
    assert first == b"\x11" * 32
    assert second == b"\x22" * 32
    assert first != second
    assert fake.calls == 2


@pytest.mark.parametrize("length", (1, 7, 32, 65), ids=("one", "seven", "thirty-two", "sixty-five"))
def test_nonempty_protected_lookup_lengths_are_accepted(length: int) -> None:
    protect = _require_symbol(
        _ADMISSION_MODULE,
        "protect_idempotency_lookup",
        "algorithm-independent protected lookup bytes",
    )
    expected = b"x" * length
    protector = _ReturningProtector(expected)

    protected = protect(
        protector,
        caller_scope="scope-a",
        raw_idempotency_key=_RAW_KEY_MARKER,
    )
    candidate = _candidate(idempotency_lookup=protected)

    assert type(protected) is bytes
    assert protected == expected
    assert candidate.idempotency_lookup == expected
    assert protector.calls == 1


@pytest.mark.parametrize(
    "invalid_lookup",
    (
        b"",
        "text",
        bytearray(b"bytes"),
        memoryview(b"bytes"),
        _BytesSubclass(b"bytes"),
    ),
    ids=("empty", "text", "bytearray", "memoryview", "bytes-subclass"),
)
def test_invalid_protected_lookup_values_are_rejected(invalid_lookup: object) -> None:
    protect = _require_symbol(
        _ADMISSION_MODULE,
        "protect_idempotency_lookup",
        "strict nonempty built-in protected lookup bytes",
    )
    protector = _ReturningProtector(invalid_lookup)

    with pytest.raises((TypeError, ValueError)):
        protect(
            protector,
            caller_scope="scope-a",
            raw_idempotency_key=_RAW_KEY_MARKER,
        )
    with pytest.raises((TypeError, ValueError)):
        _candidate(idempotency_lookup=cast(bytes, invalid_lookup))

    assert protector.calls == 1


def test_log_record_leak_detection_is_fail_closed_and_diagnostic_safe() -> None:
    message_record = logging.LogRecord(
        "red-a", logging.ERROR, __file__, 1, _RAW_KEY_MARKER, (), None
    )
    argument_record = logging.LogRecord(
        "red-a",
        logging.ERROR,
        __file__,
        1,
        "boundary value: %s",
        (_RAW_KEY_MARKER,),
        None,
    )
    extra_record = logging.LogRecord(
        "red-a", logging.ERROR, __file__, 1, "safe boundary message", (), None
    )
    extra_record.protected_boundary_detail = _RAW_KEY_MARKER
    rendered_record = logging.LogRecord(
        "red-a",
        logging.ERROR,
        __file__,
        1,
        "raw-key-%s",
        ("red-a-marker-never-render",),
        None,
    )

    for record in (message_record, argument_record, extra_record, rendered_record):
        captured = _capture_sensitive_failure(_assert_log_records_are_safe, [record])
        _assert_generic_sensitive_failure(captured)


def test_structural_scanner_rejects_custom_values_without_rendering_them() -> None:
    probe = _HostileRenderingProbe()

    captured = _capture_sensitive_failure(_assert_marker_absent, probe)

    _assert_generic_sensitive_failure(captured)
    assert probe.str_calls == 0
    assert probe.repr_calls == 0


def test_structural_scanner_handles_supported_values_cycles_and_bounds() -> None:
    recursive_list: list[object] = ["safe"]
    recursive_list.append(recursive_list)
    recursive_dict: dict[str, object] = {"safe": True}
    recursive_dict["self"] = recursive_dict
    marker_free_values = (
        "safe",
        b"safe",
        bytearray(b"safe"),
        memoryview(b"safe"),
        None,
        False,
        7,
        1.5,
        ("safe",),
        ["safe"],
        {"safe": "value"},
        {"safe"},
        frozenset({"safe"}),
        recursive_list,
        recursive_dict,
    )

    _assert_marker_absent(marker_free_values)
    encoded_marker = _RAW_KEY_MARKER.encode("utf-8")
    for leaked_value in (
        _RAW_KEY_MARKER,
        encoded_marker,
        bytearray(encoded_marker),
        memoryview(encoded_marker),
    ):
        captured = _capture_sensitive_failure(_assert_marker_absent, leaked_value)
        _assert_generic_sensitive_failure(captured)

    too_deep: object = "safe"
    for _ in range(_MAX_SCAN_DEPTH + 1):
        too_deep = [too_deep]
    too_many = ["safe"] * (_MAX_SCAN_NODES + 1)
    for bounded_failure in (too_deep, too_many):
        captured = _capture_sensitive_failure(_assert_marker_absent, bounded_failure)
        _assert_generic_sensitive_failure(captured)


def test_exception_graph_scanner_covers_all_paths_without_rendering() -> None:
    argument_error = ValueError(_RAW_KEY_MARKER)
    attribute_error = ValueError("safe")
    attribute_error.__dict__["protected_boundary_detail"] = _RAW_KEY_MARKER
    cause_error: RuntimeError | None = None
    context_error: RuntimeError | None = None

    try:
        raise ValueError(_RAW_KEY_MARKER)
    except ValueError as cause:
        try:
            raise RuntimeError("safe outer") from cause
        except RuntimeError as raised_error:
            cause_error = raised_error

    try:
        raise ValueError(_RAW_KEY_MARKER)
    except ValueError:
        try:
            raise RuntimeError("safe outer")
        except RuntimeError as raised_error:
            context_error = raised_error

    assert cause_error is not None
    assert context_error is not None
    group_error = ExceptionGroup("safe group", [ValueError(_RAW_KEY_MARKER)])
    for graph_error in (
        argument_error,
        attribute_error,
        cause_error,
        context_error,
        group_error,
    ):
        captured = _capture_sensitive_failure(_assert_exception_graph_is_safe, graph_error)
        _assert_generic_sensitive_failure(captured)

    safe_inner = ValueError("safe inner")
    safe_outer = RuntimeError("safe outer")
    safe_outer.__cause__ = safe_inner
    safe_inner.__context__ = safe_outer
    _assert_exception_graph_is_safe(safe_outer)

    rejected_input = "invalid-surrogate-\ud800"
    escaped_input = rejected_input.encode("unicode_escape").decode("ascii")
    captured = _capture_sensitive_failure(
        _assert_exception_graph_with_sensitive_value,
        ValueError(escaped_input),
        rejected_input,
    )
    _assert_generic_sensitive_failure(captured)


def test_protector_failure_exposes_no_raw_key_in_complete_exception_graph() -> None:
    protect = _require_symbol(
        _ADMISSION_MODULE,
        "protect_idempotency_lookup",
        "safe protector failure handling",
    )
    captured: Exception | None = None
    try:
        protect(
            _FailingProtector(),
            caller_scope="scope-a",
            raw_idempotency_key=_RAW_KEY_MARKER,
        )
    except Exception as error:
        captured = error
    if captured is None:
        pytest.fail("A failing protector did not raise a safe public error.", pytrace=False)

    _assert_exception_graph_is_safe(captured)


@pytest.mark.parametrize(
    "error",
    (
        RuntimeError("unexpected runtime failure"),
        AttributeError("unexpected attribute failure"),
        TypeError("unexpected type failure"),
        ValueError("unexpected value failure"),
        asyncio.CancelledError(),
        KeyboardInterrupt(),
    ),
    ids=("runtime", "attribute", "type", "value", "cancellation", "keyboard-interrupt"),
)
def test_unexpected_protector_failures_propagate_by_identity(error: BaseException) -> None:
    protect = _require_symbol(
        _ADMISSION_MODULE,
        "protect_idempotency_lookup",
        "unexpected protector failure propagation",
    )
    protector = _RaisingProtector(error)
    captured: BaseException | None = None

    try:
        protect(
            protector,
            caller_scope="scope-a",
            raw_idempotency_key=_RAW_KEY_MARKER,
        )
    except BaseException as raised:
        captured = raised

    assert captured is error
    assert protector.calls == 1


def test_domain_values_are_immutable_and_retain_no_raw_key_or_json_preimage() -> None:
    job = _job()
    candidate = _candidate()
    job_parameters = inspect.signature(type(job)).parameters
    candidate_parameters = inspect.signature(type(candidate)).parameters
    forbidden = {
        "idempotency_key",
        "raw_idempotency_key",
        "caller_scope",
        "canonical_json",
        "request_schema_version",
        "requested_analysis_version",
        "resolved_analysis_version",
        "fingerprint_version",
    }

    assert "raw_idempotency_key" not in job_parameters
    assert "raw_idempotency_key" not in candidate_parameters
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD for parameter in job_parameters.values()
    )
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD
        for parameter in candidate_parameters.values()
    )
    _assert_forbidden_public_fields_absent(job, forbidden)
    _assert_forbidden_public_fields_absent(candidate, forbidden)
    assert type(candidate.idempotency_lookup) is bytes
    assert candidate.idempotency_lookup
    assert candidate.idempotency_lookup == b"\x11" * 32
    assert candidate.request_fingerprint == _BASELINE_DIGEST
    assert type(candidate.request_fingerprint) is bytes
    assert len(candidate.request_fingerprint) == 32
    for value in (job, candidate):
        with pytest.raises((AttributeError, TypeError)):
            value.video_id = "changed"


def test_admission_outcome_taxonomy_is_exact_and_immutable() -> None:
    job = _job()
    accepted_type = _require_symbol(
        _DOMAIN_MODULE, "AnalysisJobAccepted", "the accepted admission outcome"
    )
    existing_type = _require_symbol(
        _DOMAIN_MODULE, "AnalysisJobExisting", "the existing admission outcome"
    )
    conflict_type = _require_symbol(
        _DOMAIN_MODULE,
        "AnalysisJobIdempotencyConflict",
        "the conflicting admission outcome",
    )
    rejected_type = _require_symbol(
        _DOMAIN_MODULE, "AnalysisJobCapacityRejected", "the capacity admission outcome"
    )
    outcomes = (
        accepted_type(job=job),
        existing_type(job=job),
        conflict_type(existing_job_id=_JOB_ID),
        rejected_type(),
    )

    assert outcomes[0].job is job
    assert outcomes[1].job is job
    assert outcomes[2].existing_job_id == _JOB_ID
    assert type(outcomes[3]) is rejected_type
    _assert_forbidden_public_fields_absent(
        outcomes[3], {"job", "existing_job_id", "reason", "retryable"}
    )
    for outcome in outcomes:
        with pytest.raises((AttributeError, TypeError)):
            outcome.unexpected = True


def test_public_field_checks_support_frozen_slotted_values() -> None:
    value = _FrozenSlottedValue(public_value="safe")

    assert not hasattr(value, "__dict__")
    _assert_forbidden_public_fields_absent(value, {"forbidden"})
    with pytest.raises(AssertionError, match="^unexpected public field$"):
        _assert_forbidden_public_fields_absent(value, {"public_value"})


def test_repository_port_exposes_only_accept_or_get() -> None:
    repository_type = _require_symbol(
        _ADMISSION_MODULE,
        "AnalysisJobAdmissionRepository",
        "the one-operation admission repository port",
    )
    public_methods = {
        name
        for name, value in vars(repository_type).items()
        if not name.startswith("_") and callable(value)
    }
    parameters = inspect.signature(repository_type.accept_or_get).parameters

    assert getattr(repository_type, "_is_protocol", False) is True
    assert public_methods == {"accept_or_get"}
    assert tuple(parameters) == ("self", "candidate", "max_queue_size")
    assert parameters["max_queue_size"].kind is inspect.Parameter.KEYWORD_ONLY
    assert not hasattr(repository_type, "get_by_job_id")


def test_repository_and_admission_return_only_approved_outcomes() -> None:
    repository_type = _require_symbol(
        _ADMISSION_MODULE,
        "AnalysisJobAdmissionRepository",
        "the precisely typed admission repository outcome",
    )
    admit = _require_symbol(
        _ADMISSION_MODULE,
        "admit_analysis_job",
        "the precisely typed admission outcome",
    )
    approved = {
        _require_symbol(_DOMAIN_MODULE, "AnalysisJobAccepted", "approved outcomes"),
        _require_symbol(_DOMAIN_MODULE, "AnalysisJobExisting", "approved outcomes"),
        _require_symbol(_DOMAIN_MODULE, "AnalysisJobIdempotencyConflict", "approved outcomes"),
        _require_symbol(_DOMAIN_MODULE, "AnalysisJobCapacityRejected", "approved outcomes"),
    }
    repository_return = get_type_hints(repository_type.accept_or_get)["return"]
    admission_return = get_type_hints(admit)["return"]

    assert set(get_args(repository_return)) == approved
    assert set(get_args(admission_return)) == approved


@pytest.mark.parametrize(
    "invalid_capacity",
    (True, False, 0, -1, 1.0, None),
    ids=("true", "false", "zero", "negative", "float", "none"),
)
def test_invalid_queue_capacity_fails_before_repository_io(
    invalid_capacity: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_order_spies(monkeypatch, events)
    repository = _RepositorySpy(events)
    protector = _ReturningProtector(b"protected", events)

    with pytest.raises((TypeError, ValueError)):
        _run_admission(repository, protector, max_queue_size=invalid_capacity)

    assert events == []
    assert protector.calls == 0
    assert repository.calls == 0
    assert repository.protected_lookups == []


def test_invalid_request_fails_before_protector_or_repository_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_order_spies(monkeypatch, events)
    repository = _RepositorySpy(events)
    protector = _ReturningProtector(b"protected", events)
    fragmented_callback = _request("https://example.com/callback#fragment").callback_url

    with pytest.raises(ValueError):
        _run_admission(repository, protector, callback_url=fragmented_callback)

    assert events == []
    assert protector.calls == 0
    assert repository.calls == 0


def test_expected_protector_failure_stops_admission_and_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_order_spies(monkeypatch, events)
    repository = _RepositorySpy(events)
    protector = _FailingProtector(events)
    captured: ValueError | None = None

    try:
        _run_admission(repository, protector)
    except ValueError as error:
        captured = error
    if captured is None:
        pytest.fail("Expected protector failure was not raised.", pytrace=False)

    assert captured.args == ("Idempotency lookup protection failed.",)
    _assert_exception_graph_is_safe(captured)
    assert events == ["protect"]
    assert protector.calls == 1
    assert repository.calls == 0


@pytest.mark.parametrize(
    "invalid_output",
    (b"", "not-binary", bytearray(b"not-exact"), memoryview(b"not-exact")),
    ids=("empty", "text", "bytearray", "memoryview"),
)
def test_invalid_protector_output_stops_before_repository_io(
    invalid_output: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_order_spies(monkeypatch, events)
    repository = _RepositorySpy(events)
    protector = _ReturningProtector(invalid_output, events)

    with pytest.raises((TypeError, ValueError)):
        _run_admission(repository, protector)

    assert events == ["protect"]
    assert protector.calls == 1
    assert repository.calls == 0


def test_valid_admission_orchestrates_in_order_and_returns_exact_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admit = _require_symbol(
        _ADMISSION_MODULE,
        "admit_analysis_job",
        "the complete bounded admission orchestration",
    )
    events: list[str] = []
    _install_order_spies(monkeypatch, events)
    repository = _RepositorySpy(events)
    protected_lookup = b"protected-lookup"
    protector = _ReturningProtector(protected_lookup, events)
    parameters = inspect.signature(admit).parameters

    outcome = _run_admission(repository, protector, max_queue_size=3)

    assert tuple(parameters) == (
        "repository",
        "protector",
        "job_id",
        "caller_scope",
        "raw_idempotency_key",
        "video_id",
        "player_id",
        "video_reference",
        "callback_url",
        "max_queue_size",
    )
    assert all(
        parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters.values()
    )
    assert events == ["protect", "fingerprint", "candidate", "repository"]
    assert protector.calls == 1
    _assert_marker_absent(repository.protected_lookups)
    assert outcome is repository.outcome
    assert repository.calls == 1
    assert repository.max_queue_sizes == [3]
    assert repository.protected_lookups == [protected_lookup]
    assert len(repository.candidates) == 1
    candidate = cast(Any, repository.candidates[0])
    assert type(candidate) is _require_symbol(
        _DOMAIN_MODULE,
        "NewAnalysisJob",
        "the protected immutable admission candidate",
    )
    assert candidate.idempotency_lookup == protected_lookup
    assert candidate.request_fingerprint == _reference_fingerprint()
    _assert_forbidden_public_fields_absent(
        candidate,
        {"idempotency_key", "raw_idempotency_key", "caller_scope", "canonical_json"},
    )
    assert parameters["max_queue_size"].default is inspect.Parameter.empty


@pytest.mark.parametrize(
    "invalid_fingerprint",
    (b"", b"\x00" * 31, b"\x00" * 33, "not-binary"),
    ids=("empty", "short", "long", "text"),
)
def test_invalid_fingerprint_cannot_reach_repository(invalid_fingerprint: object) -> None:
    repository = _RepositorySpy()

    with pytest.raises((TypeError, ValueError)):
        _candidate(request_fingerprint=invalid_fingerprint)  # type: ignore[arg-type]

    assert repository.calls == 0
