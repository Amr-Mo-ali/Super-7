"""Database-free scheduling and restart controls for the one-actor dispatcher."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from test_durable_role_cleanup import _assert_no_cycle, _linked

from services import durable_callback_dispatcher as dispatcher_module
from services.analysis_callback_delivery import (
    CallbackDeliveryRepository,
    CallbackTransport,
    DeliveryOutcome,
    StoredCallbackEvent,
    deliver_one,
)
from services.durable_callback_dispatcher import (
    CallbackDispatchOwnershipUnavailable,
    DurableCallbackDispatcher,
)


class _Ownership:
    def __init__(self) -> None:
        self.owned = True

    async def verify(self) -> None:
        if not self.owned:
            raise CallbackDispatchOwnershipUnavailable("ownership lost")


def _event() -> StoredCallbackEvent:
    return StoredCallbackEvent(
        UUID(int=1),
        UUID(int=2),
        "http://127.0.0.1/callback",
        "analysis_callback_snapshot_v1",
        {
            "request_id": str(UUID(int=2)),
            "video_id": "video",
            "player_id": "player",
            "status": "COMPLETED",
        },
        "PENDING",
        datetime.now(UTC),
        True,
        "COMPLETED",
        "COMPLETED",
        "SUCCEEDED",
        "video",
        "player",
    )


class _Repository:
    def __init__(self) -> None:
        self.event = _event()
        self.retry_delays: list[int] = []
        self.delivered = asyncio.Event()
        self.retried = asyncio.Event()
        self.error: BaseException | None = None

    async def next_due_event_id(self) -> UUID | None:
        if self.error is not None:
            raise self.error
        return self.event.event_id if self.event.due else None

    async def load(self, event_id: UUID) -> StoredCallbackEvent | None:
        assert event_id == self.event.event_id
        return self.event

    async def record_delivered(self, event_id: UUID) -> bool:
        assert event_id == self.event.event_id
        self.event = replace(self.event, delivery_state="DELIVERED", due=False)
        self.delivered.set()
        return True

    async def record_retry(self, event_id: UUID, *, delay_ms: int) -> bool:
        assert event_id == self.event.event_id
        self.retry_delays.append(delay_ms)
        self.event = replace(self.event, delivery_state="RETRYING", due=False)
        self.retried.set()
        return True


async def _deliver_with_short_deadline(
    *,
    repository: CallbackDeliveryRepository,
    transport: CallbackTransport,
    event_id: UUID,
    timeout_seconds: float,
    retry_delay_ms: int,
    on_transport_failure: Callable[[OSError | TimeoutError], bool] | None = None,
) -> DeliveryOutcome:
    assert timeout_seconds == 10
    return await deliver_one(
        repository=repository,
        transport=transport,
        event_id=event_id,
        timeout_seconds=0.1,
        retry_delay_ms=retry_delay_ms,
        on_transport_failure=on_transport_failure,
    )


def _dispatcher(repository: _Repository, status: int) -> DurableCallbackDispatcher:
    async def transport(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        assert url == repository.event.callback_url
        assert str(repository.event.event_id).encode() in body
        assert headers["X-Super7-Callback-Event-Id"] == str(repository.event.event_id)
        assert timeout == 10
        return status

    return DurableCallbackDispatcher(
        repository=repository,
        transport=transport,
        ownership=_Ownership(),
        logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
    )


def test_due_retry_uses_persisted_30_second_schedule_and_restart_delivers() -> None:
    async def scenario() -> None:
        repository = _Repository()
        first = _dispatcher(repository, 503)
        first.start()
        try:
            await asyncio.wait_for(repository.retried.wait(), 1)
            assert repository.retry_delays == [30_000]
            assert repository.event.delivery_state == "RETRYING"
            assert not repository.event.due
        finally:
            await asyncio.wait_for(first.stop(), 1)
        assert first.failure is None
        repository.event = replace(repository.event, due=True)
        second = _dispatcher(repository, 204)
        second.start()
        try:
            await asyncio.wait_for(repository.delivered.wait(), 1)
            assert repository.event.delivery_state == "DELIVERED"
            assert not repository.event.due
        finally:
            await asyncio.wait_for(second.stop(), 1)
        assert second.failure is None

    asyncio.run(scenario())


def test_two_dispatchers_do_not_send_the_same_pending_event_concurrently() -> None:
    async def scenario() -> None:
        repository = _Repository()
        sends: list[bytes] = []
        entered = asyncio.Event()
        release = asyncio.Event()

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            sends.append(body)
            entered.set()
            await release.wait()
            return 204

        owners = [_Ownership(), _Ownership()]
        owners[1].owned = False
        actors = [
            DurableCallbackDispatcher(
                repository=repository,
                transport=transport,
                ownership=owners[index],
                logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
            )
            for index in range(2)
        ]
        for actor in actors:
            actor.start()
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await asyncio.sleep(0)
            assert len(sends) == 1
        finally:
            release.set()
            await asyncio.wait_for(
                asyncio.gather(*(actor.stop() for actor in actors), return_exceptions=True), 2
            )
        assert isinstance(actors[1].failure, CallbackDispatchOwnershipUnavailable)

    asyncio.run(scenario())


def test_discovery_failure_is_visible_and_preserves_identity() -> None:
    async def scenario() -> None:
        repository = _Repository()
        error = RuntimeError("private-detail")
        repository.error = error
        dispatcher = _dispatcher(repository, 204)
        dispatcher.start()
        await asyncio.sleep(0)
        with pytest.raises(RuntimeError) as captured:
            await asyncio.wait_for(dispatcher.stop(), 1)
        assert captured.value is error
        assert dispatcher.failure is error

    asyncio.run(scenario())


def test_ownership_lost_before_send_stops_without_http() -> None:
    async def scenario() -> None:
        ownership = _Ownership()
        sends = 0

        class LosingRepository(_Repository):
            async def load(self, event_id: UUID) -> StoredCallbackEvent | None:
                ownership.owned = False
                return await super().load(event_id)

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            nonlocal sends
            sends += 1
            return 204

        repository = LosingRepository()
        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            ownership=ownership,
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        dispatcher.start()
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await asyncio.wait_for(dispatcher.wait(), 1)
        assert sends == 0
        assert not repository.delivered.is_set()
        assert dispatcher._ownership_uncertain

    asyncio.run(scenario())


def test_ownership_lost_during_http_stops_before_delivery_write() -> None:
    async def scenario() -> None:
        ownership = _Ownership()
        repository = _Repository()
        sends = 0

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            nonlocal sends
            sends += 1
            ownership.owned = False
            return 204

        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            ownership=ownership,
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        dispatcher.start()
        with pytest.raises(CallbackDispatchOwnershipUnavailable):
            await asyncio.wait_for(dispatcher.wait(), 1)
        assert sends == 1
        assert not repository.delivered.is_set()

    asyncio.run(scenario())


def test_post_response_verification_deadline_stops_without_delivery_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatcher_module, "deliver_one", _deliver_with_short_deadline)

    async def scenario() -> None:
        class CountingRepository(_Repository):
            discoveries = 0

            async def next_due_event_id(self) -> UUID | None:
                self.discoveries += 1
                return await super().next_due_event_id()

        class BlockingOwnership:
            def __init__(self) -> None:
                self.checks = 0
                self.uncertain = False
                self.post_response_entered = asyncio.Event()

            async def verify(self) -> None:
                self.checks += 1
                if self.checks == 3:
                    self.post_response_entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        self.uncertain = True
                        raise

        repository = CountingRepository()
        ownership = BlockingOwnership()
        sends = 0

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            nonlocal sends
            sends += 1
            return 204

        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            ownership=ownership,
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        dispatcher.start()
        try:
            await asyncio.wait_for(ownership.post_response_entered.wait(), 1)
            with pytest.raises(TimeoutError) as observed:
                await asyncio.wait_for(dispatcher.wait(), 1)
            assert dispatcher.failure is observed.value
            assert ownership.uncertain
            assert dispatcher._ownership_uncertain
            assert repository.discoveries == 1
            assert sends == 1
            assert repository.retry_delays == []
            assert not repository.retried.is_set()
            assert not repository.delivered.is_set()
        finally:
            await asyncio.wait_for(asyncio.gather(dispatcher.stop(), return_exceptions=True), 1)

    asyncio.run(scenario())


def test_pre_send_verification_cancellation_marks_ownership_uncertain() -> None:
    async def scenario() -> None:
        entered = asyncio.Event()
        cancellations: list[asyncio.CancelledError] = []
        sends = 0

        class BlockingOwnership:
            async def verify(self) -> None:
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as error:
                    cancellations.append(error)
                    raise

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            nonlocal sends
            sends += 1
            return 204

        repository = _Repository()
        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            ownership=BlockingOwnership(),
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        task = asyncio.create_task(dispatcher._send_owned("unused", b"fixed", {}, 1))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as observed:
            await task
        assert observed.value is cancellations[0]
        assert dispatcher._ownership_uncertain
        assert sends == 0
        assert not repository.delivered.is_set()
        assert not repository.retried.is_set()

    asyncio.run(scenario())


def test_ordinary_transport_deadline_still_persists_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dispatcher_module, "deliver_one", _deliver_with_short_deadline)

    async def scenario() -> None:
        repository = _Repository()
        ownership = _Ownership()
        transport_entered = asyncio.Event()

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            transport_entered.set()
            await asyncio.Event().wait()
            return 204

        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            ownership=ownership,
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        dispatcher.start()
        try:
            await asyncio.wait_for(transport_entered.wait(), 1)
            await asyncio.wait_for(repository.retried.wait(), 1)
            assert repository.retry_delays == [30_000]
            assert repository.event.delivery_state == "RETRYING"
            assert not dispatcher._ownership_uncertain
            assert dispatcher.failure is None
            assert not repository.delivered.is_set()
        finally:
            await asyncio.wait_for(dispatcher.stop(), 1)

    asyncio.run(scenario())


def test_transport_cancellation_remains_primary_when_post_send_verification_fails() -> None:
    async def scenario() -> None:
        cancellation = asyncio.CancelledError("transport cancelled")
        ownership_error = CallbackDispatchOwnershipUnavailable("ownership uncertain")

        class FailingOwnership:
            checks = 0

            async def verify(self) -> None:
                self.checks += 1
                if self.checks == 2:
                    raise ownership_error

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            raise cancellation

        dispatcher = DurableCallbackDispatcher(
            repository=_Repository(),
            transport=transport,
            ownership=FailingOwnership(),
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        with pytest.raises(asyncio.CancelledError) as observed:
            await dispatcher._send_owned("unused", b"fixed", {}, 1)
        assert observed.value is cancellation
        assert dispatcher._ownership_uncertain
        assert _linked(cancellation, ownership_error)
        _assert_no_cycle(cancellation)

    asyncio.run(scenario())


def test_task_cancellation_during_transport_keeps_exact_error_and_cleanup_link() -> None:
    async def scenario() -> None:
        ownership_error = CallbackDispatchOwnershipUnavailable("ownership uncertain")
        entered = asyncio.Event()
        cancellations: list[asyncio.CancelledError] = []

        class FailingOwnership:
            checks = 0

            async def verify(self) -> None:
                self.checks += 1
                if self.checks == 2:
                    raise ownership_error

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as error:
                cancellations.append(error)
                raise
            return 204

        dispatcher = DurableCallbackDispatcher(
            repository=_Repository(),
            transport=transport,
            ownership=FailingOwnership(),
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        task = asyncio.create_task(dispatcher._send_owned("unused", b"fixed", {}, 1))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as observed:
            await task
        assert observed.value is cancellations[0]
        assert dispatcher._ownership_uncertain
        assert _linked(observed.value, ownership_error)
        _assert_no_cycle(observed.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("fatal_transport", [False, True])
def test_transport_failure_remains_primary_when_post_send_verification_fails(
    fatal_transport: bool,
) -> None:
    async def scenario() -> None:
        primary: BaseException = (
            SystemExit("fatal transport") if fatal_transport else OSError("transport")
        )
        ownership_error = CallbackDispatchOwnershipUnavailable("ownership uncertain")

        class FailingOwnership:
            checks = 0

            async def verify(self) -> None:
                self.checks += 1
                if self.checks == 2:
                    raise ownership_error

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            raise primary

        dispatcher = DurableCallbackDispatcher(
            repository=_Repository(),
            transport=transport,
            ownership=FailingOwnership(),
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        with pytest.raises(SystemExit if fatal_transport else OSError) as observed:
            await dispatcher._send_owned("unused", b"fixed", {}, 1)
        assert observed.value is primary
        assert dispatcher._ownership_uncertain
        assert _linked(primary, ownership_error)
        _assert_no_cycle(primary)

    asyncio.run(scenario())


def test_fatal_verification_takes_precedence_over_ordinary_transport_failure() -> None:
    async def scenario() -> None:
        primary = OSError("transport")
        fatal = SystemExit("verification failed fatally")

        class FailingOwnership:
            checks = 0

            async def verify(self) -> None:
                self.checks += 1
                if self.checks == 2:
                    raise fatal

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            raise primary

        dispatcher = DurableCallbackDispatcher(
            repository=_Repository(),
            transport=transport,
            ownership=FailingOwnership(),
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        with pytest.raises(SystemExit) as observed:
            await dispatcher._send_owned("unused", b"fixed", {}, 1)
        assert observed.value is fatal
        assert dispatcher._ownership_uncertain
        assert _linked(fatal, primary)
        _assert_no_cycle(fatal)

    asyncio.run(scenario())


def test_transport_failure_with_lost_ownership_stops_without_retry_write() -> None:
    async def scenario() -> None:
        primary = OSError("transport failed")
        ownership_error = CallbackDispatchOwnershipUnavailable("ownership lost")
        repository = _Repository()
        sends = 0

        class LosingOwnership:
            checks = 0

            async def verify(self) -> None:
                self.checks += 1
                if self.checks == 3:
                    raise ownership_error

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            nonlocal sends
            sends += 1
            raise primary

        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            ownership=LosingOwnership(),
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        dispatcher.start()
        with pytest.raises(OSError) as observed:
            await asyncio.wait_for(dispatcher.wait(), 1)
        assert observed.value is primary
        assert dispatcher._ownership_uncertain
        assert _linked(primary, ownership_error)
        _assert_no_cycle(primary)
        assert sends == 1
        assert repository.retry_delays == []
        assert not repository.retried.is_set()

    asyncio.run(scenario())


def test_ambiguous_delivery_write_waits_30_seconds_before_rediscovery() -> None:
    async def scenario() -> None:
        failure = RuntimeError("private-persistence-detail")
        sends = 0

        class FailingRepository(_Repository):
            async def record_retry(self, event_id: UUID, *, delay_ms: int) -> bool:
                assert event_id == self.event.event_id
                assert delay_ms == 30_000
                raise failure

        repository = FailingRepository()

        async def transport(
            url: str, body: bytes, headers: Mapping[str, str], timeout: float
        ) -> int:
            nonlocal sends
            sends += 1
            return 503

        dispatcher = DurableCallbackDispatcher(
            repository=repository,
            transport=transport,
            ownership=_Ownership(),
            logger=logging.getLogger("football_analysis.tests.durable_dispatcher"),
        )
        observed_waits: list[float] = []
        wait_entered = asyncio.Event()

        async def wait(seconds: float) -> None:
            observed_waits.append(seconds)
            wait_entered.set()
            await dispatcher._stop.wait()

        dispatcher._wait = wait  # type: ignore[method-assign]
        dispatcher.start()
        await asyncio.wait_for(wait_entered.wait(), 1)
        await asyncio.wait_for(dispatcher.stop(), 1)
        assert observed_waits == [30]
        assert sends == 1
        assert dispatcher.last_delivery_error is failure

    asyncio.run(scenario())
