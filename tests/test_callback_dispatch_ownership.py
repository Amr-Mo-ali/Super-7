"""Deterministic deadlines and exception precedence for the dedicated session."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest
from psycopg import AsyncConnection
from test_durable_role_cleanup import _assert_no_cycle, _linked

from adapters.psycopg_callback_dispatch_ownership import (
    _ACQUIRE,
    _VERIFY,
    PsycopgCallbackDispatchOwnership,
)
from core.database_config import DatabasePoolSettings, DatabaseSettings
from services.durable_callback_dispatcher import CallbackDispatchOwnershipUnavailable


def _settings() -> DatabaseSettings:
    return DatabaseSettings(
        _host="localhost",
        _port=5432,
        _database="test-owned",
        _user="test",
        _password="test-only",
        connect_timeout_seconds=2,
        pool=DatabasePoolSettings(0, 1, 0.05, 1, 0.05, 0.05),
        required_schema_version="test",
    )


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def fetchone(self) -> tuple[object, ...] | None:
        if self.connection.fetch_error is not None:
            raise self.connection.fetch_error
        return (True,)

    async def close(self) -> None:
        if self.connection.cursor_close_error is not None:
            raise self.connection.cursor_close_error


class _Connection:
    def __init__(self) -> None:
        self.closed = False
        self.queries: list[str] = []
        self.entered = asyncio.Event()
        self.hang_query: str | None = None
        self.hang_close = False
        self.execute_error: BaseException | None = None
        self.fetch_error: BaseException | None = None
        self.cursor_close_error: BaseException | None = None
        self.connection_close_error: BaseException | None = None
        self.close_calls = 0

    async def execute(self, query: str, params: tuple[int, int]) -> _Cursor:
        assert params == (1_396_113_410, 2)
        self.queries.append(query)
        self.entered.set()
        if self.hang_query == query:
            await asyncio.Event().wait()
        if self.execute_error is not None:
            raise self.execute_error
        return _Cursor(self)

    async def close(self) -> None:
        self.close_calls += 1
        if self.hang_close:
            await asyncio.Event().wait()
        if self.connection_close_error is not None:
            raise self.connection_close_error
        self.closed = True


def _patch_connect(monkeypatch: pytest.MonkeyPatch, connection: _Connection) -> None:
    async def connect(*args: object, **kwargs: object) -> AsyncConnection[tuple[object, ...]]:
        assert kwargs["autocommit"] is True
        return cast(AsyncConnection[tuple[object, ...]], connection)

    monkeypatch.setattr(AsyncConnection, "connect", connect)


def test_acquisition_query_deadline_settles_connection_and_disables_reacquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    connection.hang_query = _ACQUIRE
    _patch_connect(monkeypatch, connection)
    owner = PsycopgCallbackDispatchOwnership(_settings())

    async def run() -> None:
        with pytest.raises(TimeoutError):
            await owner.acquire()
        assert connection.close_calls == 1 and connection.closed
        with pytest.raises(RuntimeError, match="cannot be reacquired"):
            await owner.acquire()
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await owner.verify()

    asyncio.run(run())


def test_acquisition_cancellation_attempts_session_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    connection.hang_query = _ACQUIRE
    _patch_connect(monkeypatch, connection)
    owner = PsycopgCallbackDispatchOwnership(_settings())

    async def run() -> None:
        task = asyncio.create_task(owner.acquire())
        await connection.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert connection.close_calls == 1 and connection.closed
        with pytest.raises(RuntimeError, match="cannot be reacquired"):
            await owner.acquire()

    asyncio.run(run())


def test_verification_query_deadline_stops_authority_without_requery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    _patch_connect(monkeypatch, connection)
    owner = PsycopgCallbackDispatchOwnership(_settings())

    async def run() -> None:
        await owner.acquire()
        connection.hang_query = _VERIFY
        with pytest.raises(CallbackDispatchOwnershipUnavailable) as observed:
            await owner.verify()
        assert isinstance(observed.value.__cause__, TimeoutError)
        assert connection.queries == [_ACQUIRE, _VERIFY]
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await owner.verify()
        assert connection.queries == [_ACQUIRE, _VERIFY]
        await owner.close()
        assert connection.closed

    asyncio.run(run())


def test_verification_cancellation_disables_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    _patch_connect(monkeypatch, connection)
    owner = PsycopgCallbackDispatchOwnership(_settings())

    async def run() -> None:
        await owner.acquire()
        connection.hang_query = _VERIFY
        connection.entered.clear()
        task = asyncio.create_task(owner.verify())
        await connection.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await owner.verify()
        await owner.close()

    asyncio.run(run())


@pytest.mark.parametrize("fatal_cleanup", [False, True])
def test_acquisition_cleanup_preserves_primary_or_fatal_failure(
    monkeypatch: pytest.MonkeyPatch, fatal_cleanup: bool
) -> None:
    connection = _Connection()
    primary = asyncio.CancelledError("acquire cancelled")
    cleanup: BaseException = (
        SystemExit("fatal close") if fatal_cleanup else RuntimeError("close failed")
    )
    prior_primary = LookupError("earlier acquisition cause")
    prior_cleanup = ValueError("earlier cleanup cause")
    primary.__cause__ = prior_primary
    cleanup.__cause__ = prior_cleanup
    connection.execute_error = primary
    connection.connection_close_error = cleanup
    _patch_connect(monkeypatch, connection)
    owner = PsycopgCallbackDispatchOwnership(_settings())

    async def run() -> None:
        with pytest.raises(SystemExit if fatal_cleanup else asyncio.CancelledError) as observed:
            await owner.acquire()
        expected = cleanup if fatal_cleanup else primary
        assert observed.value is expected
        assert _linked(expected, primary)
        assert _linked(expected, cleanup)
        assert _linked(expected, prior_primary)
        assert _linked(expected, prior_cleanup)
        _assert_no_cycle(expected)
        assert connection.close_calls == 1 and not connection.closed
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await owner.verify()

    asyncio.run(run())


@pytest.mark.parametrize("fatal_cleanup", [False, True])
def test_verification_read_and_cursor_cleanup_failures_keep_links(
    monkeypatch: pytest.MonkeyPatch, fatal_cleanup: bool
) -> None:
    connection = _Connection()
    _patch_connect(monkeypatch, connection)
    owner = PsycopgCallbackDispatchOwnership(_settings())
    primary = OSError("fetch failed")
    cleanup: BaseException = (
        SystemExit("fatal cursor close") if fatal_cleanup else RuntimeError("cursor close failed")
    )

    async def run() -> None:
        await owner.acquire()
        connection.fetch_error = primary
        connection.cursor_close_error = cleanup
        with pytest.raises(
            SystemExit if fatal_cleanup else CallbackDispatchOwnershipUnavailable
        ) as observed:
            await owner.verify()
        raised = observed.value
        if fatal_cleanup:
            assert raised is cleanup
        assert _linked(raised, primary)
        assert _linked(raised, cleanup)
        _assert_no_cycle(raised)
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await owner.verify()
        connection.cursor_close_error = None
        await owner.close()

    asyncio.run(run())


def test_close_deadline_does_not_claim_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection()
    _patch_connect(monkeypatch, connection)
    owner = PsycopgCallbackDispatchOwnership(_settings())

    async def run() -> None:
        await owner.acquire()
        connection.hang_close = True
        with pytest.raises(TimeoutError):
            await owner.close()
        assert not connection.closed
        assert owner._connection is cast(AsyncConnection[tuple[object, ...]], connection)
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await owner.verify()
        connection.hang_close = False
        await owner.close()
        assert connection.closed

    asyncio.run(run())
