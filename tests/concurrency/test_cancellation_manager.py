"""Deterministic unit tests for request-scoped cancellation intent."""

from dataclasses import FrozenInstanceError
from threading import Barrier, Thread

import pytest

from concurrency.cancellation import CancellationManager, CancellationState


def test_initial_state_is_active_and_snapshot_is_immutable() -> None:
    manager = CancellationManager("analysis-1")

    snapshot = manager.snapshot()

    assert snapshot.request_id == "analysis-1"
    assert snapshot.state is CancellationState.ACTIVE
    assert snapshot.cancelled is False
    with pytest.raises(FrozenInstanceError):
        snapshot.request_id = "different"  # type: ignore[misc]


def test_cancellation_request_is_observable_and_idempotent() -> None:
    manager = CancellationManager("analysis-2")

    manager.request_cancellation()
    manager.request_cancellation()

    assert manager.snapshot().state is CancellationState.CANCELLATION_REQUESTED
    assert manager.is_cancelled() is True
    assert manager.wait(timeout=0) is True


def test_deadline_expiration_is_observable_and_idempotent() -> None:
    manager = CancellationManager("analysis-3")

    manager.expire_deadline()
    manager.expire_deadline()

    assert manager.snapshot().state is CancellationState.DEADLINE_EXPIRED
    assert manager.is_cancelled() is True


def test_shutdown_request_is_observable_and_idempotent() -> None:
    manager = CancellationManager("analysis-4")

    manager.request_shutdown()
    manager.request_shutdown()

    assert manager.snapshot().state is CancellationState.SHUTDOWN_REQUESTED
    assert manager.is_cancelled() is True


def test_completion_is_idempotent_and_stops_cancellation_observation() -> None:
    manager = CancellationManager("analysis-5")

    manager.complete()
    manager.complete()

    assert manager.snapshot().state is CancellationState.COMPLETED
    assert manager.is_cancelled() is False
    assert manager.wait(timeout=0) is False


def test_conflicting_transition_fails_loudly() -> None:
    manager = CancellationManager("analysis-6")
    manager.request_cancellation()

    with pytest.raises(RuntimeError, match="Illegal cancellation transition"):
        manager.expire_deadline()


def test_concurrent_same_transition_is_safe_and_deterministic() -> None:
    manager = CancellationManager("analysis-7")
    barrier = Barrier(8)
    errors: list[BaseException] = []

    def request_cancellation() -> None:
        try:
            barrier.wait()
            manager.request_cancellation()
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [Thread(target=request_cancellation) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert manager.snapshot().state is CancellationState.CANCELLATION_REQUESTED


def test_state_is_isolated_between_requests() -> None:
    first = CancellationManager("analysis-a")
    second = CancellationManager("analysis-b")

    first.request_shutdown()

    assert first.is_cancelled() is True
    assert second.snapshot().state is CancellationState.ACTIVE
    assert second.is_cancelled() is False


def test_complete_after_cancellation_is_legal() -> None:
    manager = CancellationManager("analysis-8")
    manager.request_cancellation()

    manager.complete()

    assert manager.snapshot().state is CancellationState.COMPLETED
