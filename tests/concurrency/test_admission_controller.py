"""Deterministic unit tests for local analysis admission control."""

import asyncio

import pytest

from concurrency.admission import AdmissionController


def test_successful_admission_and_idempotent_release() -> None:
    async def scenario() -> None:
        controller = AdmissionController(max_active_analyses=2)

        permit = await controller.admit()

        assert permit is not None
        metrics = await controller.metrics()
        assert metrics.max_active_analyses == 2
        assert metrics.active_permits == 1
        assert metrics.admitted_analyses == 1
        assert metrics.rejected_analyses == 0
        await permit.release()
        await permit.release()
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_capacity_exhaustion_does_not_start_rejected_analysis() -> None:
    async def scenario() -> None:
        controller = AdmissionController(max_active_analyses=1)
        accepted = await controller.admit()
        rejected = await controller.admit()
        analysis_started = False

        if rejected is not None:
            analysis_started = True

        assert accepted is not None
        assert rejected is None
        assert analysis_started is False
        metrics = await controller.metrics()
        assert metrics.max_active_analyses == 1
        assert metrics.active_permits == 1
        assert metrics.admitted_analyses == 1
        assert metrics.rejected_analyses == 1
        await accepted.release()

    asyncio.run(scenario())


def test_concurrent_admission_never_exceeds_capacity() -> None:
    async def scenario() -> None:
        controller = AdmissionController(max_active_analyses=3)

        permits = await asyncio.gather(*(controller.admit() for _ in range(8)))

        accepted = [permit for permit in permits if permit is not None]
        assert len(accepted) == 3
        assert (await controller.metrics()).active_permits == 3
        await asyncio.gather(*(permit.release() for permit in accepted))
        metrics = await controller.metrics()
        assert metrics.max_active_analyses == 3
        assert metrics.active_permits == 0
        assert metrics.admitted_analyses == 3
        assert metrics.rejected_analyses == 5

    asyncio.run(scenario())


def test_context_manager_releases_permit_after_exception() -> None:
    async def scenario() -> None:
        controller = AdmissionController(max_active_analyses=1)
        permit = await controller.admit()

        assert permit is not None
        with pytest.raises(RuntimeError, match="analysis failed"):
            async with permit:
                raise RuntimeError("analysis failed")
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_context_manager_releases_permit_after_cancellation() -> None:
    async def scenario() -> None:
        controller = AdmissionController(max_active_analyses=1)
        entered = asyncio.Event()
        never_complete = asyncio.Event()

        async def admitted_work() -> None:
            permit = await controller.admit()
            assert permit is not None
            async with permit:
                entered.set()
                await never_complete.wait()

        task = asyncio.create_task(admitted_work())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await controller.metrics()).active_permits == 0

    asyncio.run(scenario())


def test_capacity_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        AdmissionController(max_active_analyses=0)
