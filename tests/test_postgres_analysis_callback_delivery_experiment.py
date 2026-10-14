"""Opt-in one-event-at-a-time PostgreSQL and loopback HTTP delivery evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from collections.abc import Mapping
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Literal, cast
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

import pytest
from psycopg import sql
from psycopg.errors import InsufficientPrivilege
from test_postgres_analysis_job_finalization_integration import _finalizer, _snapshot
from test_postgres_analysis_job_worker_integration import (
    _connect,
    _current_settings,
    _disposable_factory,
    _fetch_all,
    _grant_runtime_worker_privileges,
    _prepare_database,
    _repositories,
    _run_database,
)

from adapters.psycopg_analysis_callback_delivery_repository import (
    PsycopgAnalysisCallbackDeliveryRepository,
)
from core.database_config import DatabaseSettings
from domain.analysis_job import AnalysisJobAccepted, NewAnalysisJob
from domain.analysis_job_attempt import LeaseDuration, WorkerClaim, WorkerSlotId
from services.analysis_callback_delivery import CallbackTransport, deliver_one
from services.analysis_job_finalization import AnalysisJobCompleted
from services.callback_service import _RejectRedirects

_TIMEOUT_SECONDS = 2.0
_RETRY_DELAY_MS = 500
_EVIDENCE_QUERY = """SELECT j.state, a.status, r.terminal_kind,
                            j.*, a.*, r.*, o.callback_event_id, o.job_id,
                            o.payload_schema_version, o.payload_json, o.created_at,
                            o.delivery_state, o.next_attempt_at
                     FROM analysis_jobs AS j
                     JOIN analysis_job_attempts AS a ON a.job_id = j.job_id
                     JOIN analysis_job_terminals AS r ON r.job_id = j.job_id
                     JOIN analysis_callback_outbox AS o ON o.job_id = j.job_id
                     WHERE j.job_id = %s"""


class _Receiver:
    def __init__(self) -> None:
        self.requests: list[tuple[str, bytes, dict[str, str]]] = []
        self.applied: dict[str, bytes] = {}
        self.fail_first = False
        self.conflicts = 0
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers["Content-Length"])
                body = self.rfile.read(size)
                event_id = self.headers.get("X-Super7-Callback-Event-Id", "")
                headers = {
                    "Content-Type": self.headers.get("Content-Type", ""),
                    "Accept": self.headers.get("Accept", ""),
                    "X-Super7-Callback-Event-Id": event_id,
                }
                receiver.requests.append((self.path, body, headers))
                parsed = json.loads(body)
                if parsed.get("callbackEventId") != event_id:
                    status = 409
                elif receiver.fail_first:
                    receiver.fail_first = False
                    status = 503
                elif event_id in receiver.applied and receiver.applied[event_id] != body:
                    receiver.conflicts += 1
                    status = 409
                else:
                    receiver.applied[event_id] = body
                    status = 204
                self.send_response(status)
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/callback"

    def close(self) -> None:
        errors: list[BaseException] = []

        for close_step in (
            self.server.shutdown,
            self.server.server_close,
            lambda: self.thread.join(timeout=5),
        ):
            try:
                close_step()
            except BaseException as error:
                errors.append(error)
        try:
            if self.thread.is_alive():
                raise AssertionError("Task-owned callback receiver thread remains alive.")
        except BaseException as error:
            errors.append(error)

        if errors:
            fatal_index = next(
                (index for index, error in enumerate(errors) if not isinstance(error, Exception)),
                None,
            )
            if fatal_index is not None:
                fatal = errors[fatal_index]
                related = errors[:fatal_index] + errors[fatal_index + 1 :]
                active_primary = sys.exception()
                for linked in (fatal.__cause__, fatal.__context__):
                    if (
                        linked is not None
                        and linked is not active_primary
                        and all(existing is not linked for existing in related)
                    ):
                        related.append(linked)
                if active_primary is not None:
                    for observed in related:
                        if observed.__cause__ is active_primary:
                            observed.__cause__ = None
                        if observed.__context__ is active_primary:
                            observed.__context__ = None
                if len(related) == 1:
                    raise fatal from related[0]
                if related:
                    raise fatal from BaseExceptionGroup("Receiver cleanup failures.", related)
                raise fatal
            if len(errors) == 1 and sys.exception() is None:
                raise errors[0]
            raise BaseExceptionGroup("Receiver cleanup failures.", errors)


async def _local_transport(
    url: str, body: bytes, headers: Mapping[str, str], timeout: float
) -> int:
    # This isolated test transport allows only the dynamic task-owned loopback server.
    assert url.startswith("http://127.0.0.1:") and url.endswith("/callback")

    def post() -> int:
        request = Request(url, data=body, headers=dict(headers), method="POST")
        try:
            with build_opener(ProxyHandler({}), _RejectRedirects()).open(
                request, timeout=timeout
            ) as response:
                return int(response.status)
        except HTTPError as error:
            return int(error.code)

    return await asyncio.to_thread(post)


def _delivery_grant(settings: DatabaseSettings, runtime: DatabaseSettings, grant: bool) -> None:
    with _connect(settings) as connection:
        action = sql.SQL("GRANT") if grant else sql.SQL("REVOKE")
        relation = sql.SQL("TO") if grant else sql.SQL("FROM")
        connection.execute(
            sql.SQL(
                "{} UPDATE (delivery_state, next_attempt_at) ON analysis_callback_outbox {} {}"
            ).format(action, relation, sql.Identifier(runtime._user))
        ).close()


def _delivery_privileges(runtime: DatabaseSettings, *, update: bool) -> None:
    with _connect(runtime) as connection:
        row = connection.execute(
            """SELECT has_column_privilege(current_user, 'analysis_callback_outbox',
                       'delivery_state', 'UPDATE'),
                      has_column_privilege(current_user, 'analysis_callback_outbox',
                       'next_attempt_at', 'UPDATE'),
                      has_column_privilege(current_user, 'analysis_callback_outbox',
                       'payload_json', 'UPDATE'),
                      has_table_privilege(current_user, 'analysis_callback_outbox', 'DELETE')"""
        ).fetchone()
    assert row == (update, update, False, False)


def _controlled_receiver(
    *,
    shutdown_error: BaseException | None = None,
    close_error: BaseException | None = None,
    join_error: BaseException | None = None,
    alive_error: BaseException | None = None,
    alive: bool = False,
) -> tuple[_Receiver, list[str]]:
    calls: list[str] = []

    class Server:
        def shutdown(self) -> None:
            calls.append("shutdown")
            if shutdown_error is not None:
                raise shutdown_error

        def server_close(self) -> None:
            calls.append("server_close")
            if close_error is not None:
                raise close_error

    class Thread:
        def join(self, timeout: float | None = None) -> None:
            calls.append("join")
            assert timeout == 5
            if join_error is not None:
                raise join_error

        def is_alive(self) -> bool:
            calls.append("is_alive")
            if alive_error is not None:
                raise alive_error
            return alive

    receiver = object.__new__(_Receiver)
    receiver.server = cast(HTTPServer, Server())
    receiver.thread = cast(threading.Thread, Thread())
    return receiver, calls


@pytest.mark.parametrize("failed_step", ("shutdown", "server_close"))
def test_receiver_cleanup_continues_after_server_failure(failed_step: str) -> None:
    first = RuntimeError(f"{failed_step}-failed")
    receiver, calls = _controlled_receiver(
        shutdown_error=first if failed_step == "shutdown" else None,
        close_error=first if failed_step == "server_close" else None,
        alive=True,
    )
    with pytest.raises(ExceptionGroup) as captured:
        receiver.close()
    assert calls == ["shutdown", "server_close", "join", "is_alive"]
    assert captured.value.exceptions[0] is first
    assert isinstance(captured.value.exceptions[1], AssertionError)
    assert "remains alive" in str(captured.value.exceptions[1])
    assert all(error.__context__ is None for error in captured.value.exceptions)


def test_receiver_cleanup_success_and_multiple_failures_without_primary() -> None:
    finished, calls = _controlled_receiver()
    finished.close()
    assert calls == ["shutdown", "server_close", "join", "is_alive"]

    close_error = RuntimeError("server-close-failed")
    join_error = RuntimeError("join-failed")
    liveness_error = RuntimeError("liveness-failed")
    failing, calls = _controlled_receiver(
        close_error=close_error, join_error=join_error, alive_error=liveness_error
    )
    with pytest.raises(ExceptionGroup) as captured:
        failing.close()
    assert calls == ["shutdown", "server_close", "join", "is_alive"]
    assert captured.value.exceptions == (close_error, join_error, liveness_error)


def test_receiver_cleanup_preserves_independent_shutdown_cause() -> None:
    underlying = ValueError("independent-shutdown-cause")
    shutdown_error = RuntimeError("shutdown-failed")
    shutdown_error.__cause__ = underlying
    receiver, calls = _controlled_receiver(shutdown_error=shutdown_error)

    with pytest.raises(RuntimeError) as captured:
        receiver.close()
    assert captured.value is shutdown_error
    assert shutdown_error.__cause__ is underlying
    assert calls == ["shutdown", "server_close", "join", "is_alive"]


def test_receiver_cleanup_first_fatal_retains_later_failures() -> None:
    first = KeyboardInterrupt("shutdown-fatal")
    original_cause = ValueError("independent-fatal-cause")
    first.__cause__ = original_cause
    second = SystemExit("server-close-fatal")
    join_error = RuntimeError("join-failed")
    receiver, calls = _controlled_receiver(
        shutdown_error=first,
        close_error=second,
        join_error=join_error,
        alive=True,
    )

    with pytest.raises(KeyboardInterrupt) as captured:
        receiver.close()
    assert captured.value is first
    assert calls == ["shutdown", "server_close", "join", "is_alive"]
    related = first.__cause__
    assert isinstance(related, BaseExceptionGroup)
    assert related.exceptions[:2] == (second, join_error)
    assert isinstance(related.exceptions[2], AssertionError)
    assert related.exceptions[3] is original_cause


def test_receiver_cleanup_preserves_active_primary_and_fatal_precedence() -> None:
    class FailingDatabase:
        def __init__(self, primary: BaseException) -> None:
            self.primary = primary

        def __enter__(self) -> None:
            raise self.primary

        def __exit__(self, *args: object) -> Literal[False]:
            return False

    primary = AssertionError("primary-test-failure")
    ordinary = RuntimeError("shutdown-failed")
    receiver, calls = _controlled_receiver(shutdown_error=ordinary, alive=True)
    with (
        patch.object(sys.modules[__name__], "_Receiver", return_value=receiver),
        patch.object(
            sys.modules[__name__],
            "_disposable_factory",
            return_value=lambda: FailingDatabase(primary),
        ),
        pytest.raises(AssertionError) as captured,
    ):
        test_one_event_http_retry_ambiguous_ack_and_persistence_failure(
            cast(pytest.FixtureRequest, object())
        )
    assert captured.value is primary
    assert calls == ["shutdown", "server_close", "join", "is_alive"]
    related = primary.__cause__
    assert isinstance(related, ExceptionGroup)
    assert related.__context__ is None
    assert related.exceptions[0] is ordinary
    assert isinstance(related.exceptions[1], AssertionError)
    assert all(error.__context__ is None for error in related.exceptions)

    fatal = KeyboardInterrupt("fatal-shutdown")
    later = RuntimeError("server-close-failed")
    fatal_receiver, fatal_calls = _controlled_receiver(
        shutdown_error=fatal, close_error=later, alive=True
    )
    with (
        patch.object(sys.modules[__name__], "_Receiver", return_value=fatal_receiver),
        patch.object(
            sys.modules[__name__],
            "_disposable_factory",
            return_value=lambda: FailingDatabase(primary),
        ),
        pytest.raises(KeyboardInterrupt) as captured_fatal,
    ):
        test_one_event_http_retry_ambiguous_ack_and_persistence_failure(
            cast(pytest.FixtureRequest, object())
        )
    assert captured_fatal.value is fatal
    assert fatal_calls == ["shutdown", "server_close", "join", "is_alive"]
    assert fatal.__context__ is primary
    fatal_related = fatal.__cause__
    assert isinstance(fatal_related, ExceptionGroup)
    assert fatal_related.exceptions[0] is later
    assert isinstance(fatal_related.exceptions[1], AssertionError)
    assert all(error.__context__ is None for error in fatal_related.exceptions)


def test_receiver_cleanup_retains_unrelated_context_and_linked_failures_with_primary() -> None:
    class FailingDatabase:
        def __init__(self, primary: AssertionError) -> None:
            self.primary = primary

        def __enter__(self) -> None:
            raise self.primary

        def __exit__(self, *args: object) -> Literal[False]:
            return False

    primary = AssertionError("primary")
    underlying = ValueError("independent-cleanup-context")
    cleanup = RuntimeError("cleanup")

    class ContextualReceiver:
        def close(self) -> None:
            try:
                raise underlying
            except ValueError:
                raise cleanup from None

    with (
        patch.object(sys.modules[__name__], "_Receiver", return_value=ContextualReceiver()),
        patch.object(
            sys.modules[__name__],
            "_disposable_factory",
            return_value=lambda: FailingDatabase(primary),
        ),
        pytest.raises(AssertionError) as captured,
    ):
        test_one_event_http_retry_ambiguous_ack_and_persistence_failure(
            cast(pytest.FixtureRequest, object())
        )
    assert captured.value is primary
    assert primary.__cause__ is cleanup
    assert cleanup.__cause__ is None
    assert cleanup.__context__ is underlying
    assert underlying.__context__ is None

    linked_primary = AssertionError("linked-primary")
    cause = ValueError("independent-shutdown-cause")
    linked = RuntimeError("shutdown-failed")
    linked.__cause__ = cause
    later = RuntimeError("server-close-failed")
    receiver, calls = _controlled_receiver(shutdown_error=linked, close_error=later, alive=True)
    with (
        patch.object(sys.modules[__name__], "_Receiver", return_value=receiver),
        patch.object(
            sys.modules[__name__],
            "_disposable_factory",
            return_value=lambda: FailingDatabase(linked_primary),
        ),
        pytest.raises(AssertionError) as captured_linked,
    ):
        test_one_event_http_retry_ambiguous_ack_and_persistence_failure(
            cast(pytest.FixtureRequest, object())
        )
    assert captured_linked.value is linked_primary
    assert calls == ["shutdown", "server_close", "join", "is_alive"]
    related = linked_primary.__cause__
    assert isinstance(related, ExceptionGroup)
    assert related.__context__ is None
    assert related.exceptions[0] is linked
    assert related.exceptions[1] is later
    assert isinstance(related.exceptions[2], AssertionError)
    assert linked.__cause__ is cause
    assert linked.__context__ is None
    assert cause.__context__ is None
    assert later.__context__ is None


def test_loopback_receiver_acknowledges_identical_repeat_and_rejects_conflict() -> None:
    receiver = _Receiver()
    try:
        event_id = str(uuid4())
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Super7-Callback-Event-Id": event_id,
        }
        body = json.dumps({"callbackEventId": event_id, "status": "COMPLETED"}).encode()

        async def scenario() -> None:
            assert await _local_transport(receiver.url, body, headers, 2) == 204
            assert await _local_transport(receiver.url, body, headers, 2) == 204
            assert (
                await _local_transport(
                    receiver.url,
                    json.dumps({"callbackEventId": event_id, "status": "failed"}).encode(),
                    headers,
                    2,
                )
                == 409
            )

        asyncio.run(scenario())
        assert receiver.applied == {event_id: body}
        assert receiver.conflicts == 1
        assert len(receiver.requests) == 3
    finally:
        receiver.close()
    assert not receiver.thread.is_alive()


@pytest.mark.integration
def test_one_event_http_retry_ambiguous_ack_and_persistence_failure(
    request: pytest.FixtureRequest,
) -> None:
    factory = _disposable_factory(request)
    receiver = _Receiver()
    primary: BaseException | None = None
    try:
        with factory() as database:
            _prepare_database(database)
            _grant_runtime_worker_privileges(database.migration_settings, database.runtime_settings)
            _delivery_grant(database.migration_settings, database.runtime_settings, True)
            _delivery_privileges(database.runtime_settings, update=True)
            settings = _current_settings(database.runtime_settings)

            async def experiment() -> None:
                async with _repositories(settings) as (admission, worker, pool):
                    delivery = PsycopgAnalysisCallbackDeliveryRepository(
                        pool, connection_acquire_timeout_seconds=5
                    )

                    async def local_transport(
                        url: str, body: bytes, headers: Mapping[str, str], timeout: float
                    ) -> int:
                        assert url == receiver.url
                        return await _local_transport(url, body, headers, timeout)

                    async def create_event() -> tuple[UUID, UUID, dict[str, object]]:
                        job_id = uuid4()
                        candidate = NewAnalysisJob(
                            job_id=job_id,
                            idempotency_lookup=hashlib.sha256(job_id.bytes + b"lookup").digest(),
                            request_fingerprint=hashlib.sha256(
                                job_id.bytes + b"fingerprint"
                            ).digest(),
                            video_id="synthetic-video",
                            player_id="synthetic-player",
                            video_reference="synthetic.mp4",
                            callback_url=receiver.url,
                            analysis_version="1.0.0",
                            attempt_limit=1,
                        )
                        assert isinstance(
                            await admission.accept_or_get(candidate, max_queue_size=1),
                            AnalysisJobAccepted,
                        )
                        claim = await worker.claim_next(
                            worker_slot_id=WorkerSlotId(uuid4()),
                            lease_duration=LeaseDuration(timedelta(seconds=30)),
                        )
                        assert isinstance(claim, WorkerClaim)
                        assert claim.authority.job_id == job_id
                        completed = await _finalizer(pool).finalize_success(
                            authority=claim.authority, result=_snapshot(claim)
                        )
                        assert isinstance(completed, AnalysisJobCompleted)
                        rows = await _fetch_all(
                            pool,
                            _EVIDENCE_QUERY,
                            (job_id,),
                        )
                        assert len(rows) == 1
                        row = rows[0]
                        assert row[:3] == ("COMPLETED", "SUCCEEDED", "COMPLETED")
                        assert row[-7] == completed.callback_event_id and row[-2] == "PENDING"
                        assert isinstance(row[-4], dict)
                        return job_id, completed.callback_event_id, cast(dict[str, object], row[-4])

                    async def evidence(job_id: UUID) -> tuple[object, ...]:
                        rows = await _fetch_all(
                            pool,
                            _EVIDENCE_QUERY,
                            (job_id,),
                        )
                        assert len(rows) == 1
                        return rows[0]

                    async def send(
                        event_id: UUID, transport: CallbackTransport = local_transport
                    ) -> str:
                        return await deliver_one(
                            repository=delivery,
                            transport=transport,
                            event_id=event_id,
                            timeout_seconds=_TIMEOUT_SECONDS,
                            retry_delay_ms=_RETRY_DELAY_MS,
                        )

                    # A: an observed 503 changes only delivery state/due time.
                    job_a, event_a, payload_a = await create_event()
                    before_a = await evidence(job_a)
                    receiver.fail_first = True
                    assert await send(event_a) == "RETRYING"
                    retry_a = await evidence(job_a)
                    assert retry_a[:-2] == before_a[:-2]
                    assert isinstance(retry_a[-1], datetime)
                    assert isinstance(before_a[-1], datetime)
                    assert retry_a[-2] == "RETRYING" and retry_a[-1] > before_a[-1]
                    assert await send(event_a) == "NOT_DUE"
                    await asyncio.sleep(_RETRY_DELAY_MS / 1000 + 0.02)
                    assert await send(event_a) == "DELIVERED"
                    delivered_a = await evidence(job_a)
                    assert delivered_a[:-2] == before_a[:-2]
                    assert delivered_a[-2] == "DELIVERED"
                    assert delivered_a[-1] == retry_a[-1]
                    assert await send(event_a) == "ALREADY_DELIVERED"
                    assert (
                        receiver.applied[str(event_a)]
                        == json.dumps(
                            {**payload_a, "callbackEventId": str(event_a)},
                            separators=(",", ":"),
                            allow_nan=False,
                        ).encode()
                    )

                    # B: receiver commits, but the first transport loses acknowledgement.
                    job_b, event_b, _ = await create_event()
                    before_b = await evidence(job_b)
                    lost = False

                    async def lost_ack(
                        url: str, body: bytes, headers: Mapping[str, str], timeout: float
                    ) -> int:
                        nonlocal lost
                        status = await local_transport(url, body, headers, timeout)
                        if not lost:
                            lost = True
                            raise TimeoutError("test-owned lost acknowledgement")
                        return status

                    assert await send(event_b, lost_ack) == "RETRYING"
                    assert str(event_b) in receiver.applied
                    await asyncio.sleep(_RETRY_DELAY_MS / 1000 + 0.02)
                    assert await send(event_b) == "DELIVERED"
                    after_b = await evidence(job_b)
                    assert after_b[:-2] == before_b[:-2] and after_b[-2] == "DELIVERED"

                    # C: real HTTP acceptance precedes a real restricted-role UPDATE failure.
                    job_c, event_c, _ = await create_event()
                    before_c = await evidence(job_c)
                    _delivery_grant(database.migration_settings, database.runtime_settings, False)
                    _delivery_privileges(database.runtime_settings, update=False)
                    try:
                        with pytest.raises(InsufficientPrivilege):
                            await send(event_c)
                        assert str(event_c) in receiver.applied
                        assert await evidence(job_c) == before_c
                    finally:
                        _delivery_grant(
                            database.migration_settings, database.runtime_settings, True
                        )
                    _delivery_privileges(database.runtime_settings, update=True)
                    assert await send(event_c) == "DELIVERED"
                    after_c = await evidence(job_c)
                    assert after_c[:-2] == before_c[:-2] and after_c[-2] == "DELIVERED"

                    for event_id in (event_a, event_b, event_c):
                        deliveries = [
                            (json.loads(body), headers)
                            for path, body, headers in receiver.requests
                            if path == "/callback"
                            and headers["X-Super7-Callback-Event-Id"] == str(event_id)
                        ]
                        assert len(deliveries) == 2
                        assert deliveries[0] == deliveries[1]
                        body, headers = deliveries[0]
                        assert body["callbackEventId"] == str(event_id)
                        assert headers == {
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "X-Super7-Callback-Event-Id": str(event_id),
                        }
                    assert len(receiver.applied) == 3 and receiver.conflicts == 0
                    print(
                        "callback_experiment events=3 http_requests=6 applications=3 "
                        "A=RETRYING/DELIVERED B=RETRYING/DELIVERED "
                        "C=PENDING/DELIVERED"
                    )

            _run_database(experiment())
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            receiver.close()
        except BaseException as cleanup:
            if primary is not None and isinstance(cleanup, Exception):
                if cleanup is primary:
                    raise
                # Only remove links back to the primary before attaching cleanup
                # as its cause; retain independent cleanup causes and contexts.
                pending: list[BaseException] = [cleanup]
                seen: set[int] = set()
                while pending:
                    observed = pending.pop()
                    if observed is primary or id(observed) in seen:
                        continue
                    seen.add(id(observed))
                    for link_name in ("__cause__", "__context__"):
                        linked = getattr(observed, link_name)
                        if linked is primary:
                            setattr(observed, link_name, None)
                        elif linked is not None:
                            pending.append(linked)
                    if isinstance(observed, BaseExceptionGroup):
                        pending.extend(observed.exceptions)
                raise primary from cleanup
            raise
