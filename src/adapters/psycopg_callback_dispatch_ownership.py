"""Session-scoped singleton ownership for the durable callback dispatcher.

The two-key advisory lock (1396113410, 2) is reserved for Super-7 callback
dispatch. It is distinct from the admission transaction lock (1396113410, 1).
All dispatchers sharing the same PostgreSQL database must use this identity.
"""

from __future__ import annotations

import asyncio

from psycopg import AsyncConnection

from core.database_config import DatabaseSettings
from services.cleanup_exception_links import raise_after_cleanup
from services.durable_callback_dispatcher import CallbackDispatchOwnershipUnavailable

_NAMESPACE = 1_396_113_410
_RESOURCE = 2
_ACQUIRE = "SELECT pg_try_advisory_lock(%s, %s)"
_VERIFY = """SELECT EXISTS (
    SELECT 1 FROM pg_locks
    WHERE locktype = 'advisory' AND pid = pg_backend_pid()
      AND classid = %s::oid AND objid = %s::oid
      AND objsubid = 2 AND granted
)"""


class PsycopgCallbackDispatchOwnership:
    """Hold the lock on a dedicated, never-pooled, autocommit connection.

    Pool open/acquire/close settings bound startup, verification, and settlement
    respectively. A client timeout cancels the driver operation; if the driver
    cannot settle cancellation, elapsed time may exceed that deadline.
    """

    def __init__(self, settings: DatabaseSettings) -> None:
        self._settings = settings
        self._connection: AsyncConnection[tuple[object, ...]] | None = None
        self._uncertain = False

    async def acquire(self) -> None:
        if self._connection is not None or self._uncertain:
            raise RuntimeError("Callback dispatch ownership cannot be reacquired.")
        settings = self._settings
        connection: AsyncConnection[tuple[object, ...]] | None = None
        try:
            async with asyncio.timeout(settings.pool.open_timeout_seconds):
                connection = await AsyncConnection.connect(
                    "",
                    host=settings._host,
                    port=settings._port,
                    dbname=settings._database,
                    user=settings._user,
                    password=settings._password,
                    connect_timeout=settings.connect_timeout_seconds,
                    autocommit=True,
                )
                row = await _read_bool(connection, _ACQUIRE)
            if row != (True,):
                raise CallbackDispatchOwnershipUnavailable("Callback dispatcher is already owned.")
            self._connection = connection
        except BaseException as primary:
            if not isinstance(primary, CallbackDispatchOwnershipUnavailable):
                self._uncertain = True
            if connection is not None:
                try:
                    await self._close_connection(connection)
                except BaseException as cleanup:
                    self._connection = connection
                    self._uncertain = True
                    raise_after_cleanup(primary, cleanup)
            raise

    async def verify(self) -> None:
        connection = self._connection
        if self._uncertain or connection is None or connection.closed:
            self._uncertain = True
            raise CallbackDispatchOwnershipUnavailable("Callback dispatcher ownership was lost.")
        try:
            async with asyncio.timeout(self._settings.pool.acquire_timeout_seconds):
                row = await _read_bool(connection, _VERIFY)
        except BaseException as error:
            self._uncertain = True
            if not isinstance(error, Exception):
                raise
            raise CallbackDispatchOwnershipUnavailable(
                "Callback dispatcher ownership is uncertain."
            ) from error
        if row != (True,):
            self._uncertain = True
            raise CallbackDispatchOwnershipUnavailable("Callback dispatcher ownership was lost.")

    async def close(self) -> None:
        connection, self._connection = self._connection, None
        self._uncertain = True
        if connection is not None:
            try:
                await self._close_connection(connection)
            except BaseException:
                # Release is unconfirmed. Retain the session for a later close
                # attempt, but never restore dispatch authority.
                self._connection = connection
                raise

    async def _close_connection(self, connection: AsyncConnection[tuple[object, ...]]) -> None:
        async with asyncio.timeout(self._settings.pool.close_timeout_seconds):
            await connection.close()


async def _read_bool(
    connection: AsyncConnection[tuple[object, ...]], query: str
) -> tuple[object, ...] | None:
    cursor = await connection.execute(query, (_NAMESPACE, _RESOURCE))
    try:
        row = await cursor.fetchone()
    except BaseException as primary:
        try:
            await cursor.close()
        except BaseException as cleanup:
            raise_after_cleanup(primary, cleanup)
        raise
    await cursor.close()
    return row
