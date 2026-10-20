"""Opt-in evidence for session ownership on task-owned disposable PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace

import pytest
from test_durable_callback_dispatcher import _Repository
from test_postgres_analysis_callback_delivery_experiment import _local_transport, _Receiver
from test_postgres_analysis_job_worker_integration import (
    _connect,
    _disposable_factory,
    _run_database,
)

from adapters.psycopg_callback_dispatch_ownership import PsycopgCallbackDispatchOwnership
from services.durable_callback_dispatcher import (
    CallbackDispatchOwnershipUnavailable,
    DurableCallbackDispatcher,
)


@pytest.mark.integration
def test_disposable_postgres_excludes_other_sessions_and_releases_on_close(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    receiver = _Receiver()
    try:
        with factory() as database:
            settings = database.runtime_settings

            async def scenario() -> None:
                first = PsycopgCallbackDispatchOwnership(settings)
                second = PsycopgCallbackDispatchOwnership(settings)
                repository = _Repository()
                repository.event = replace(repository.event, callback_url=receiver.url)
                await first.acquire()
                try:
                    with pytest.raises(CallbackDispatchOwnershipUnavailable):
                        await second.acquire()
                    await first.verify()
                    dispatcher = DurableCallbackDispatcher(
                        repository=repository,
                        transport=_local_transport,
                        ownership=first,
                        logger=logging.getLogger("football_analysis.tests.dispatch_ownership"),
                    )
                    dispatcher.start()
                    try:
                        await asyncio.wait_for(repository.delivered.wait(), 5)
                    finally:
                        await asyncio.wait_for(dispatcher.stop(), 5)
                    assert len(receiver.requests) == 1
                    assert json.loads(receiver.requests[0][1]) == {
                        **repository.event.payload,
                        "callbackEventId": str(repository.event.event_id),
                    }
                finally:
                    await first.close()
                await second.acquire()
                try:
                    await second.verify()
                    with pytest.raises(CallbackDispatchOwnershipUnavailable):
                        await first.verify()
                    assert repository.event.delivery_state == "DELIVERED"
                    assert len(receiver.requests) == 1
                finally:
                    await second.close()

            _run_database(scenario())
    finally:
        receiver.close()


@pytest.mark.integration
def test_terminated_task_owned_backend_loses_authority_and_next_session_acquires(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    with factory() as database:
        first = PsycopgCallbackDispatchOwnership(database.runtime_settings)
        second = PsycopgCallbackDispatchOwnership(database.runtime_settings)

        def terminate_owned_backend(pid: int) -> None:
            # A separate connection under the same task-owned runtime role may
            # terminate its own backend; no persistent or foreign PID is used.
            with _connect(database.runtime_settings) as administrator:
                cursor = administrator.execute("SELECT pg_terminate_backend(%s, %s)", (pid, 5000))
                try:
                    assert cursor.fetchone() == (True,)
                finally:
                    cursor.close()

        async def scenario() -> None:
            await first.acquire()
            try:
                connection = first._connection
                assert connection is not None
                await asyncio.to_thread(terminate_owned_backend, connection.info.backend_pid)
                with pytest.raises(CallbackDispatchOwnershipUnavailable):
                    await first.verify()
                await second.acquire()
                try:
                    await second.verify()
                    with pytest.raises(CallbackDispatchOwnershipUnavailable):
                        await first.verify()
                finally:
                    await second.close()
            finally:
                await first.close()

        _run_database(scenario())
