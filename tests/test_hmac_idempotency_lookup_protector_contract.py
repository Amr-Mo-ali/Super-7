from __future__ import annotations

import hashlib
import hmac
import importlib
import importlib.util
import inspect
import logging
import struct
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, Protocol, SupportsIndex, get_type_hints, runtime_checkable

import pytest

import main
from services.analysis_job_admission import (
    IdempotencyLookupProtector,
    protect_idempotency_lookup,
)

if TYPE_CHECKING:
    from adapters.hmac_idempotency_lookup_protector import (
        HmacIdempotencyLookupProtector,
    )

    def _typecheck_admission_boundary(
        protector: HmacIdempotencyLookupProtector,
    ) -> bytes:
        return protect_idempotency_lookup(
            protector,
            caller_scope="scope",
            raw_idempotency_key="key",
        )


_MODULE_NAME = "adapters.hmac_idempotency_lookup_protector"
_CLASS_NAME = "HmacIdempotencyLookupProtector"
_RED_MESSAGE = (
    "HMAC protector RED-E: adapters.hmac_idempotency_lookup_protector is "
    "missing; GREEN-E must implement the approved inactive HMAC protector."
)
_DOMAIN_TAG = b"super7:idempotency-lookup:v1\x00"
_SECRET_TYPE_MESSAGE = "HMAC protector secret must be exact bytes."
_SECRET_LENGTH_MESSAGE = "HMAC protector secret must be at least 32 bytes."
_SCOPE_TYPE_MESSAGE = "caller_scope must be an exact string."
_SCOPE_EMPTY_MESSAGE = "caller_scope must not be empty."
_SCOPE_ENCODING_MESSAGE = "caller_scope must be valid UTF-8."
_KEY_TYPE_MESSAGE = "raw_idempotency_key must be an exact string."
_KEY_EMPTY_MESSAGE = "raw_idempotency_key must not be empty."
_KEY_ENCODING_MESSAGE = "raw_idempotency_key must be valid UTF-8."
_SCAN_FAILURE = "Sensitive-value scan could not safely inspect the value graph."
_LEAK_FAILURE = "Sensitive data was present in the inspected value graph."
_GRAPH_FAILURE = "Exception graph could not be safely inspected."
_ANNOTATION_FAILURE = "Exact protect annotations differ."
_MAX_SCAN_DEPTH, _MAX_SCAN_NODES = 12, 192

SECRET_A, SECRET_B = bytes(range(32)), bytes(range(32, 64))
SECRET_MARKER = b"synthetic-secret-marker-00000000"


class _BytesSubclass(bytes):
    pass


class _StringSubclass(str):
    pass


class _HostileString(str):
    render_calls = 0

    def __repr__(self) -> str:
        type(self).render_calls += 1
        raise AssertionError("custom repr must not be called")

    def __str__(self) -> str:
        type(self).render_calls += 1
        raise AssertionError("custom str must not be called")


@runtime_checkable
class _ProtectorBoundary(Protocol):
    def __init__(self, secret: object) -> None: ...

    def protect(
        self,
        *,
        caller_scope: object,
        raw_idempotency_key: object,
    ) -> bytes: ...


def _require_adapter_module() -> ModuleType:
    if importlib.util.find_spec(_MODULE_NAME) is None:
        pytest.fail(_RED_MESSAGE, pytrace=False)
    return importlib.import_module(_MODULE_NAME)


def _protector_class() -> type[_ProtectorBoundary]:
    module = _require_adapter_module()
    protector_class = getattr(module, _CLASS_NAME)
    assert isinstance(protector_class, type)
    return protector_class


def _protector(secret: bytes = SECRET_A) -> _ProtectorBoundary:
    instance = _protector_class()(secret)
    assert isinstance(instance, _ProtectorBoundary)
    return instance


def _reference_frame(caller_scope: str, raw_idempotency_key: str) -> bytes:
    scope_bytes = caller_scope.encode("utf-8", "strict")
    key_bytes = raw_idempotency_key.encode("utf-8", "strict")
    return b"".join(
        (
            _DOMAIN_TAG,
            struct.pack(">Q", len(scope_bytes)),
            scope_bytes,
            struct.pack(">Q", len(key_bytes)),
            key_bytes,
        )
    )


def _assert_exact_protect_surface(protect: Callable[..., object]) -> None:
    signature = inspect.signature(protect)
    assert tuple(signature.parameters) == (
        "self",
        "caller_scope",
        "raw_idempotency_key",
    )
    for name in ("caller_scope", "raw_idempotency_key"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is inspect.Parameter.empty
    expected = {
        "caller_scope": str,
        "raw_idempotency_key": str,
        "return": bytes,
    }
    if get_type_hints(protect) != expected:
        raise AssertionError(_ANNOTATION_FAILURE)


def _assert_sensitive_absent(
    markers: Iterable[str | bytes],
    *roots: object,
) -> None:
    marker_tuple = tuple(markers)
    if not marker_tuple or any(type(marker) not in (str, bytes) for marker in marker_tuple):
        raise AssertionError(_SCAN_FAILURE)

    pending = [(root, 0) for root in roots]
    seen: set[int] = set()
    visited = 0
    while pending:
        value, depth = pending.pop()
        visited += 1
        if visited > _MAX_SCAN_NODES or depth > _MAX_SCAN_DEPTH:
            raise AssertionError(_SCAN_FAILURE)

        if type(value) is str:
            if any(type(marker) is str and marker in value for marker in marker_tuple):
                raise AssertionError(_LEAK_FAILURE)
            continue
        if type(value) is bytes:
            if any(type(marker) is bytes and marker in value for marker in marker_tuple):
                raise AssertionError(_LEAK_FAILURE)
            continue
        if isinstance(value, (bytearray, memoryview)):
            if type(value) not in (bytearray, memoryview):
                raise AssertionError(_SCAN_FAILURE)
            binary = bytes(value)
            if any(type(marker) is bytes and marker in binary for marker in marker_tuple):
                raise AssertionError(_LEAK_FAILURE)
            continue
        if value is None or type(value) in (bool, int, float):
            continue

        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(value, (tuple, list, set, frozenset)):
            if type(value) not in (tuple, list, set, frozenset):
                raise AssertionError(_SCAN_FAILURE)
            pending.extend((item, depth + 1) for item in value)
            continue
        if isinstance(value, dict):
            if type(value) is not dict:
                raise AssertionError(_SCAN_FAILURE)
            for key, item in value.items():
                pending.append((key, depth + 1))
                pending.append((item, depth + 1))
            continue
        raise AssertionError(_SCAN_FAILURE)


def _assert_exception_graph_sensitive_absent(
    markers: Iterable[str | bytes],
    error: BaseException,
) -> None:
    pending: list[tuple[BaseException, int]] = [(error, 0)]
    seen: set[int] = set()
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if visited > _MAX_SCAN_NODES or depth > _MAX_SCAN_DEPTH:
            raise AssertionError(_GRAPH_FAILURE)
        if id(current) in seen:
            continue
        seen.add(id(current))

        reduction = BaseException.__reduce__(current)
        if (
            type(reduction) is not tuple
            or len(reduction) not in (2, 3)
            or reduction[0] is not type(current)
            or type(reduction[1]) is not tuple
        ):
            raise AssertionError(_GRAPH_FAILURE)
        reduced_arguments = reduction[1]
        is_group = issubclass(type(current), BaseExceptionGroup)
        arguments = reduced_arguments[:1] if is_group else reduced_arguments
        if len(reduction) == 2:
            attributes: dict[object, object] = {}
        else:
            state = reduction[2]
            if type(state) is not dict:
                raise AssertionError(_GRAPH_FAILURE)
            attributes = state
        _assert_sensitive_absent(markers, arguments, attributes)

        cause = vars(BaseException)["__cause__"].__get__(current, type(current))
        context = vars(BaseException)["__context__"].__get__(current, type(current))
        if cause is not None:
            pending.append((cause, depth + 1))
        if context is not None:
            pending.append((context, depth + 1))
        if is_group:
            members = BaseExceptionGroup.exceptions.__get__(current, type(current))
            if type(members) is not tuple or any(
                not issubclass(type(member), BaseException) for member in members
            ):
                raise AssertionError(_GRAPH_FAILURE)
            pending.extend((member, depth + 1) for member in members)


_VECTORS = (
    (
        SECRET_A,
        "apex:tenant-123",
        "opaque-key-456",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "00000000000f617065783a74656e616e742d313233000000000000000e6f7061"
        "7175652d6b65792d343536",
        "50be5236ae9ca95db1c7ffa496393cdba9f50ae0c4e49094888d520d70d5ab90",
    ),
    (
        SECRET_A,
        "apex:tenant-124",
        "opaque-key-456",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "00000000000f617065783a74656e616e742d313234000000000000000e6f7061"
        "7175652d6b65792d343536",
        "7a1279b8fa2f3f5fbe508342444f111c0c19a962328453e4375718e185196ee8",
    ),
    (
        SECRET_A,
        "apex:tenant-123",
        "opaque-key-457",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "00000000000f617065783a74656e616e742d313233000000000000000e6f7061"
        "7175652d6b65792d343537",
        "999ea57514d251a88212be150e7a2df43b905693ec6f9b3ae9180994371305c1",
    ),
    (
        SECRET_A,
        "apex:t\u00e9nant-\u96ea",
        "opaque-key-456",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "000000000010617065783a74c3a96e616e742de99baa000000000000000e6f70"
        "617175652d6b65792d343536",
        "76686446c96c8e9d1b2e9321b479b71302dc84c6fdd6d7a5487bde5d5d75c9ec",
    ),
    (
        SECRET_A,
        "apex:tenant-123",
        "cl\u00e9-\U0001f511",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "00000000000f617065783a74656e616e742d3132330000000000000009636cc3"
        "a92df09f9491",
        "920dd49b8a12fbe3c00e9cffccf2db1fd26e9c42f81925ea898d86c5f3ffaea0",
    ),
    (
        SECRET_A,
        "ab",
        "c",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "0000000000026162000000000000000163",
        "aec536820aae423880120dcfcfb998399b5cfaaca6e7ae3041d747934610e88a",
    ),
    (
        SECRET_A,
        "a",
        "bc",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "0000000000016100000000000000026263",
        "51542d9bb3aac077a35c79e1fdd7c1da450045c9b03b7d76ee921eb1b0319635",
    ),
    (
        SECRET_B,
        "apex:tenant-123",
        "opaque-key-456",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "00000000000f617065783a74656e616e742d313233000000000000000e6f7061"
        "7175652d6b65792d343536",
        "09d07885bdb642ba81be7ca36ddeb7341e4e42b7362851e68a846c3633c737b9",
    ),
    (
        SECRET_A,
        "\u00e9",
        "key",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "000000000002c3a900000000000000036b6579",
        "e5152faf7a47d0ef3b4cb6409f8e42e61092360740dd2e74ddd9d0e3c6d5ab78",
    ),
    (
        SECRET_A,
        "e\u0301",
        "key",
        "7375706572373a6964656d706f74656e63792d6c6f6f6b75703a7631000000"
        "00000000000365cc8100000000000000036b6579",
        "925cbb6b8b26ad5001e84aef0c47a7d3ebb3ad6ea866de22f6ebab0b0a442d5d",
    ),
)


def test_helper_sensitive_scanner_is_bounded_cycle_safe_and_fail_closed() -> None:
    cycle: list[object] = []
    cycle.append(cycle)
    _assert_sensitive_absent(("marker", b"marker"), cycle)
    with pytest.raises(AssertionError, match=f"^{_LEAK_FAILURE}$"):
        _assert_sensitive_absent(("marker",), {"outer": ["marker"]})
    _HostileString.render_calls = 0
    with pytest.raises(AssertionError, match=f"^{_SCAN_FAILURE}$"):
        _assert_sensitive_absent(("marker",), _HostileString("marker"))
    assert _HostileString.render_calls == 0
    deep: object = "safe"
    for _ in range(_MAX_SCAN_DEPTH + 1):
        deep = [deep]
    with pytest.raises(AssertionError, match=f"^{_SCAN_FAILURE}$"):
        _assert_sensitive_absent(("marker",), deep)


def test_helper_representation_assertion_detects_printable_secret() -> None:
    class _DeliberatelyLeakyProtector:
        def __repr__(self) -> str:
            return f"<Protector secret={SECRET_MARKER!r}>"

        def __str__(self) -> str:
            return f"Protector(secret={SECRET_MARKER!r})"

    markers = (SECRET_MARKER.decode("ascii"), SECRET_MARKER.hex())
    leaky = _DeliberatelyLeakyProtector()
    for rendered in (repr(leaky), str(leaky)):
        with pytest.raises(AssertionError, match=f"^{_LEAK_FAILURE}$"):
            _assert_sensitive_absent(markers, rendered)


def test_helper_exception_graph_covers_links_groups_and_cycles() -> None:
    cause = ValueError("safe cause")
    context = RuntimeError("safe context")
    outer = ExceptionGroup("safe group", [LookupError("safe member")])
    outer.__cause__ = cause
    outer.__context__ = context
    cause.__context__ = outer
    _assert_exception_graph_sensitive_absent(("marker",), outer)
    leaked = RuntimeError("marker")
    wrapped = ExceptionGroup("safe", [leaked])
    with pytest.raises(AssertionError, match=f"^{_LEAK_FAILURE}$"):
        _assert_exception_graph_sensitive_absent(("marker",), wrapped)

    cause_leak = RuntimeError("safe outer")
    cause_leak.__cause__ = RuntimeError("marker in cause")
    context_leak = RuntimeError("safe outer")
    context_leak.__context__ = RuntimeError("marker in context")
    for linked in (
        RuntimeError("marker in args"),
        cause_leak,
        context_leak,
        ExceptionGroup("safe outer", [ExceptionGroup("safe nested", [leaked])]),
    ):
        with pytest.raises(AssertionError, match=f"^{_LEAK_FAILURE}$") as caught:
            _assert_exception_graph_sensitive_absent(("marker",), linked)
        assert caught.value.args == (_LEAK_FAILURE,)
        _assert_exception_graph_sensitive_absent(("marker",), caught.value)

    deep = RuntimeError("safe")
    cursor = deep
    for _ in range(_MAX_SCAN_DEPTH + 1):
        child = RuntimeError("safe")
        cursor.__cause__ = child
        cursor = child
    with pytest.raises(AssertionError, match=f"^{_GRAPH_FAILURE}$") as caught:
        _assert_exception_graph_sensitive_absent(("marker",), deep)
    assert caught.value.args == (_GRAPH_FAILURE,)
    _assert_exception_graph_sensitive_absent(("marker",), caught.value)


def test_helper_exception_attributes_are_scanned_without_rendering() -> None:
    class _AttributedError(RuntimeError):
        details: object
        secret: object
        payload: object

    marker_text = SECRET_MARKER.decode("ascii")
    markers = (SECRET_MARKER, marker_text, SECRET_MARKER.hex())
    safe = _AttributedError("fixed safe message")
    safe.details = {"classification": "safe", "attempt": 1}
    _assert_exception_graph_sensitive_absent(markers, safe)

    for leaked_secret in (SECRET_MARKER, marker_text, SECRET_MARKER.hex()):
        leaked_value = _AttributedError("fixed safe message")
        leaked_value.secret = leaked_secret
        assert leaked_value.__dict__ == {"secret": leaked_secret}
        with pytest.raises(AssertionError, match=f"^{_LEAK_FAILURE}$") as caught:
            _assert_exception_graph_sensitive_absent(markers, leaked_value)
        assert caught.value.args == (_LEAK_FAILURE,)
        _assert_exception_graph_sensitive_absent(markers, caught.value)

    leaked_name = RuntimeError("fixed safe message")
    leaked_name.__dict__[marker_text] = "safe"
    with pytest.raises(AssertionError, match=f"^{_LEAK_FAILURE}$"):
        _assert_exception_graph_sensitive_absent(markers, leaked_name)

    hostile = _AttributedError("fixed safe message")
    _HostileString.render_calls = 0
    hostile.payload = _HostileString(marker_text)
    with pytest.raises(AssertionError, match=f"^{_SCAN_FAILURE}$"):
        _assert_exception_graph_sensitive_absent(markers, hostile)
    assert _HostileString.render_calls == 0

    dictionary_calls = 0

    def hostile_dictionary(_: BaseException) -> dict[str, object]:
        nonlocal dictionary_calls
        dictionary_calls += 1
        raise RuntimeError("custom __dict__ must not be called")

    _HostileDictionaryError = type(
        "_HostileDictionaryError",
        (RuntimeError,),
        {"__dict__": property(hostile_dictionary)},
    )

    safe_hostile = _HostileDictionaryError("fixed safe message")
    BaseException.__setattr__(safe_hostile, "details", {"classification": "safe"})
    _assert_exception_graph_sensitive_absent(markers, safe_hostile)
    assert dictionary_calls == 0

    for leaked_secret in (SECRET_MARKER, marker_text, SECRET_MARKER.hex()):
        leaked_hostile = _HostileDictionaryError("fixed safe message")
        BaseException.__setattr__(leaked_hostile, "secret", leaked_secret)
        with pytest.raises(AssertionError, match=f"^{_LEAK_FAILURE}$") as caught:
            _assert_exception_graph_sensitive_absent(markers, leaked_hostile)
        assert caught.value.args == (_LEAK_FAILURE,)
        _assert_exception_graph_sensitive_absent(markers, caught.value)
        assert dictionary_calls == 0

    class _HostileReductionError(RuntimeError):
        reduce_calls = 0
        reduce_ex_calls = 0

        def __reduce__(self) -> str | tuple[object, ...]:
            type(self).reduce_calls += 1
            raise RuntimeError("custom __reduce__ must not be called")

        def __reduce_ex__(self, protocol: SupportsIndex) -> str | tuple[object, ...]:
            type(self).reduce_ex_calls += 1
            raise RuntimeError("custom __reduce_ex__ must not be called")

    hostile_reduction = _HostileReductionError("fixed safe message")
    BaseException.__setattr__(hostile_reduction, "details", "safe")
    _assert_exception_graph_sensitive_absent(markers, hostile_reduction)
    assert _HostileReductionError.reduce_calls == 0
    assert _HostileReductionError.reduce_ex_calls == 0

    class _HostileAttributeError(RuntimeError):
        attribute_calls = 0

        def __getattribute__(self, name: str) -> object:
            type(self).attribute_calls += 1
            raise RuntimeError("custom __getattribute__ must not be called")

    hostile_attribute = _HostileAttributeError("fixed safe message")
    BaseException.__setattr__(hostile_attribute, "details", "safe")
    _assert_exception_graph_sensitive_absent(markers, hostile_attribute)
    assert _HostileAttributeError.attribute_calls == 0


def test_helper_exact_protect_annotations_accept_exact_types_and_reject_any() -> None:
    class _CorrectlyTypedScratchProtector:
        __slots__ = ("_secret",)

        def __init__(self, secret: bytes) -> None:
            self._secret = secret

        def protect(
            self,
            *,
            caller_scope: str,
            raw_idempotency_key: str,
        ) -> bytes:
            return b"x" * 32

    class _AnyTypedScratchProtector:
        __slots__ = ("_secret",)

        def __init__(self, secret: bytes) -> None:
            self._secret = secret

        def protect(
            self,
            *,
            caller_scope: Any,
            raw_idempotency_key: Any,
        ) -> Any:
            return b"x" * 32

    _assert_exact_protect_surface(_CorrectlyTypedScratchProtector.protect)
    with pytest.raises(AssertionError, match=f"^{_ANNOTATION_FAILURE}$"):
        _assert_exact_protect_surface(_AnyTypedScratchProtector.protect)


def test_guard_existing_port_and_create_app_surface_are_unchanged() -> None:
    port_signature = inspect.signature(IdempotencyLookupProtector.protect)
    assert tuple(port_signature.parameters) == (
        "self",
        "caller_scope",
        "raw_idempotency_key",
    )
    assert port_signature.parameters["caller_scope"].kind is inspect.Parameter.KEYWORD_ONLY
    assert port_signature.parameters["raw_idempotency_key"].kind is inspect.Parameter.KEYWORD_ONLY
    assert tuple(inspect.signature(main.create_app).parameters) == tuple(
        "settings tracker selector validator lifecycle downloader path_resolver "
        "callback_service analysis_queue process_analysis_pool".split()
    )


def test_guard_runtime_has_no_hmac_adapter_state_or_dependency() -> None:
    state_values = tuple(main.app.state._state.values())
    assert all(type(value).__module__ != _MODULE_NAME for value in state_values)
    pending = [getattr(route, "dependant", None) for route in main.app.routes]
    seen: set[int] = set()
    while pending:
        dependency = pending.pop()
        if dependency is None or id(dependency) in seen:
            continue
        seen.add(id(dependency))
        call = getattr(dependency, "call", None)
        assert getattr(call, "__module__", None) != _MODULE_NAME
        pending.extend(getattr(dependency, "dependencies", ()))


def test_guard_application_import_does_not_load_hmac_adapter() -> None:
    script = """
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / "src"))
import main
assert "adapters.hmac_idempotency_lookup_protector" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0


def test_future_public_surface_and_signatures_are_exact() -> None:
    module = _require_adapter_module()
    protector_class = _protector_class()
    slots_name, protect_name = "__slots__", "protect"
    assert module.__all__ == [_CLASS_NAME]
    assert getattr(protector_class, slots_name) == ("_secret",)

    constructor = inspect.signature(protector_class.__init__)
    assert tuple(constructor.parameters) == ("self", "secret")
    assert constructor.parameters["secret"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert constructor.parameters["secret"].default is inspect.Parameter.empty
    assert get_type_hints(protector_class.__init__) == {"secret": bytes, "return": type(None)}
    _assert_exact_protect_surface(getattr(protector_class, protect_name))
    public_callables = {
        name
        for name in dir(protector_class)
        if not name.startswith("_") and callable(getattr(protector_class, name))
    }
    assert public_callables == {"protect"}


@pytest.mark.parametrize(
    "secret",
    [SECRET_A, SECRET_A + b"x", b"x" * 65, b"x" * 4097],
    ids=["32", "33", "65", "4097"],
)
def test_valid_secret_lengths_are_accepted(secret: bytes) -> None:
    caller_scope = "apex:tenant-123"
    raw_key = "opaque-key-456"
    frame = _reference_frame(caller_scope, raw_key)
    expected = hmac.new(secret, frame, hashlib.sha256).digest()
    output = _protector(secret).protect(
        caller_scope=caller_scope,
        raw_idempotency_key=raw_key,
    )
    assert type(output) is bytes
    assert output == expected


@pytest.mark.parametrize(
    ("secret", "error_type", "message"),
    [
        (b"", ValueError, _SECRET_LENGTH_MESSAGE),
        (b"x", ValueError, _SECRET_LENGTH_MESSAGE),
        (b"x" * 16, ValueError, _SECRET_LENGTH_MESSAGE),
        (b"x" * 31, ValueError, _SECRET_LENGTH_MESSAGE),
        ("secret", TypeError, _SECRET_TYPE_MESSAGE),
        (bytearray(SECRET_A), TypeError, _SECRET_TYPE_MESSAGE),
        (memoryview(SECRET_A), TypeError, _SECRET_TYPE_MESSAGE),
        (_BytesSubclass(SECRET_A), TypeError, _SECRET_TYPE_MESSAGE),
        (None, TypeError, _SECRET_TYPE_MESSAGE),
        (True, TypeError, _SECRET_TYPE_MESSAGE),
        (32, TypeError, _SECRET_TYPE_MESSAGE),
        (object(), TypeError, _SECRET_TYPE_MESSAGE),
    ],
    ids=lambda _: "case",
)
def test_invalid_secrets_are_rejected(
    secret: object,
    error_type: type[Exception],
    message: str,
) -> None:
    protector_class = _protector_class()
    with pytest.raises(error_type) as caught:
        protector_class(secret)
    assert caught.value.args == (message,)


@pytest.mark.parametrize(
    ("caller_scope", "error_type", "message"),
    [
        ("", ValueError, _SCOPE_EMPTY_MESSAGE),
        (_StringSubclass("scope"), TypeError, _SCOPE_TYPE_MESSAGE),
        (None, TypeError, _SCOPE_TYPE_MESSAGE),
        (True, TypeError, _SCOPE_TYPE_MESSAGE),
        (1, TypeError, _SCOPE_TYPE_MESSAGE),
        (b"scope", TypeError, _SCOPE_TYPE_MESSAGE),
        (object(), TypeError, _SCOPE_TYPE_MESSAGE),
        ("\ud800", ValueError, _SCOPE_ENCODING_MESSAGE),
    ],
    ids=lambda _: "case",
)
def test_invalid_caller_scope_is_rejected_independently(
    caller_scope: object,
    error_type: type[Exception],
    message: str,
) -> None:
    protector = _protector()
    with pytest.raises(error_type) as caught:
        protector.protect(caller_scope=caller_scope, raw_idempotency_key="valid")
    assert caught.value.args == (message,)


@pytest.mark.parametrize(
    ("raw_key", "error_type", "message"),
    [
        ("", ValueError, _KEY_EMPTY_MESSAGE),
        (_StringSubclass("key"), TypeError, _KEY_TYPE_MESSAGE),
        (None, TypeError, _KEY_TYPE_MESSAGE),
        (True, TypeError, _KEY_TYPE_MESSAGE),
        (1, TypeError, _KEY_TYPE_MESSAGE),
        (b"key", TypeError, _KEY_TYPE_MESSAGE),
        (object(), TypeError, _KEY_TYPE_MESSAGE),
        ("\ud800", ValueError, _KEY_ENCODING_MESSAGE),
    ],
    ids=lambda _: "case",
)
def test_invalid_raw_key_is_rejected_independently(
    raw_key: object,
    error_type: type[Exception],
    message: str,
) -> None:
    protector = _protector()
    with pytest.raises(error_type) as caught:
        protector.protect(caller_scope="valid", raw_idempotency_key=raw_key)
    assert caught.value.args == (message,)


@pytest.mark.parametrize(
    ("caller_scope", "raw_key"),
    [(" \t ", "k" * 4097), ("s" * 4097, "raw\x00key")],
    ids=("whitespace-and-long-key", "long-scope-and-nul-key"),
)
def test_valid_special_inputs_are_preserved(caller_scope: str, raw_key: str) -> None:
    _require_adapter_module()
    frame = _reference_frame(caller_scope, raw_key)
    expected = hmac.new(SECRET_A, frame, hashlib.sha256).digest()
    output = _protector().protect(
        caller_scope=caller_scope,
        raw_idempotency_key=raw_key,
    )
    assert output == expected


@pytest.mark.parametrize(
    ("upper_scope", "lower_scope", "upper_key", "lower_key"),
    [
        ("Apex:Tenant-123", "apex:tenant-123", "opaque-key", "opaque-key"),
        ("apex:tenant", "apex:tenant", "Opaque-Key-456", "opaque-key-456"),
    ],
    ids=("caller-scope", "raw-key"),
)
def test_case_is_preserved_independently(
    upper_scope: str,
    lower_scope: str,
    upper_key: str,
    lower_key: str,
) -> None:
    upper_frame = _reference_frame(upper_scope, upper_key)
    lower_frame = _reference_frame(lower_scope, lower_key)
    assert upper_frame != lower_frame

    upper_expected = hmac.new(SECRET_A, upper_frame, hashlib.sha256).digest()
    lower_expected = hmac.new(SECRET_A, lower_frame, hashlib.sha256).digest()
    assert upper_expected != lower_expected

    protector = _protector()
    upper_output = protector.protect(
        caller_scope=upper_scope,
        raw_idempotency_key=upper_key,
    )
    lower_output = protector.protect(
        caller_scope=lower_scope,
        raw_idempotency_key=lower_key,
    )
    assert upper_output == upper_expected
    assert lower_output == lower_expected
    assert upper_output != lower_output


@pytest.mark.parametrize(
    ("secret", "caller_scope", "raw_key", "frame_hex", "digest_hex"),
    _VECTORS,
    ids=lambda _: "case",
)
def test_frozen_vectors_match_independent_frame_hmac_and_adapter(
    secret: bytes,
    caller_scope: str,
    raw_key: str,
    frame_hex: str,
    digest_hex: str,
) -> None:
    _require_adapter_module()
    frame = _reference_frame(caller_scope, raw_key)
    assert frame.hex() == frame_hex
    expected = hmac.new(secret, frame, hashlib.sha256).digest()
    assert expected.hex() == digest_hex
    output = _protector(secret).protect(
        caller_scope=caller_scope,
        raw_idempotency_key=raw_key,
    )
    assert type(output) is bytes
    assert output == expected
    assert len(output) == hashlib.sha256().digest_size


def test_determinism_isolation_and_opaque_raw_output() -> None:
    scope = "apex:tenant-123"
    key = "opaque-key-456"
    frame = _reference_frame(scope, key)
    first = _protector(SECRET_A)
    second = _protector(SECRET_A)
    different_secret = _protector(SECRET_B)
    baseline = first.protect(caller_scope=scope, raw_idempotency_key=key)
    assert first.protect(caller_scope=scope, raw_idempotency_key=key) == baseline
    assert second.protect(caller_scope=scope, raw_idempotency_key=key) == baseline
    assert different_secret.protect(caller_scope=scope, raw_idempotency_key=key) != baseline
    assert first.protect(caller_scope=scope + "x", raw_idempotency_key=key) != baseline
    assert first.protect(caller_scope=scope, raw_idempotency_key=key + "x") != baseline
    assert type(baseline) is bytes
    assert len(baseline) == 32
    assert baseline not in (SECRET_A, scope.encode(), key.encode(), frame)
    ambiguous_a = _reference_frame("ab", "c")
    ambiguous_b = _reference_frame("a", "bc")
    assert ambiguous_a != ambiguous_b


def test_instance_surface_does_not_expose_secret_or_per_call_state() -> None:
    protector = _protector(SECRET_MARKER)
    before_names = tuple(dir(protector))
    before_repr = repr(protector)
    before_str = str(protector)
    protector.protect(caller_scope="scope-marker", raw_idempotency_key="key-marker")
    after_names = tuple(dir(protector))

    assert before_names == after_names
    assert not hasattr(protector, "__dict__")
    public_names = tuple(name for name in before_names if not name.startswith("_"))
    assert public_names == ("protect",)
    _assert_sensitive_absent(
        (
            SECRET_MARKER.decode("ascii"),
            SECRET_MARKER.hex(),
            "scope-marker",
            "key-marker",
        ),
        before_repr,
        before_str,
        public_names,
    )


@pytest.mark.parametrize("target", ["secret", "scope", "key"])
def test_validation_failures_do_not_leak_or_log_sensitive_values(
    target: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scope_marker = "sensitive-scope-marker"
    key_marker = "sensitive-key-marker"
    _HostileString.render_calls = 0
    caplog.set_level(logging.DEBUG)

    if target == "secret":
        error_type = TypeError
        message = _SECRET_TYPE_MESSAGE
        markers: tuple[str | bytes, ...] = (
            SECRET_MARKER,
            SECRET_MARKER.decode("ascii"),
            SECRET_MARKER.hex(),
        )
    elif target == "scope":
        error_type = TypeError
        message = _SCOPE_TYPE_MESSAGE
        markers = (scope_marker,)
    else:
        error_type = TypeError
        message = _KEY_TYPE_MESSAGE
        markers = (key_marker,)

    with pytest.raises(error_type) as caught:
        if target == "secret":
            _protector_class()(_BytesSubclass(SECRET_MARKER))
        elif target == "scope":
            _protector().protect(
                caller_scope=_HostileString(scope_marker),
                raw_idempotency_key="valid",
            )
        else:
            _protector().protect(
                caller_scope="valid",
                raw_idempotency_key=_HostileString(key_marker),
            )
    assert caught.value.args == (message,)
    _assert_exception_graph_sensitive_absent(markers, caught.value)
    assert caplog.records == []
    assert _HostileString.render_calls == 0


def test_adapter_import_is_environment_free_and_creates_no_instance() -> None:
    _require_adapter_module()
    script = f"""
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))

class Poison(dict):
    def __getitem__(self, key):
        raise AssertionError("environment read")
    def get(self, key, default=None):
        raise AssertionError("environment read")
    def __contains__(self, key):
        raise AssertionError("environment read")

os.environ = Poison()
module = importlib.import_module("{_MODULE_NAME}")
protector_class = getattr(module, "{_CLASS_NAME}")
assert all(not isinstance(value, protector_class) for value in vars(module).values())
assert "main" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0
