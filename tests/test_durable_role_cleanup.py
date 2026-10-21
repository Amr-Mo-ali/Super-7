"""Offline exception-identity controls for isolated role shutdown."""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

import durable_api_main
import durable_dispatcher_main
import durable_worker_main
from core.config import Settings
from services.durable_api_runtime import DurableApiRuntime
from services.durable_dispatcher_runtime import DurableDispatcherRuntime
from services.durable_worker_runtime import DurableWorkerRuntime


def _linked(root: BaseException, target: BaseException) -> bool:
    seen: set[int] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if current is target:
            return True
        if id(current) in seen:
            continue
        seen.add(id(current))
        pending.extend(
            error for error in (current.__cause__, current.__context__) if error is not None
        )
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
    return False


def _assert_no_cycle(error: BaseException) -> None:
    def visit(current: BaseException, path: frozenset[int]) -> None:
        assert id(current) not in path
        next_path = path | {id(current)}
        for linked in (current.__cause__, current.__context__):
            if linked is not None:
                visit(linked, next_path)
        if isinstance(current, BaseExceptionGroup):
            for member in current.exceptions:
                visit(member, next_path)

    visit(error, frozenset())


class _Actor:
    def __init__(self, failure: BaseException | None) -> None:
        self.failure = failure
        self.entered = asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        if self.failure is not None:
            raise self.failure
        await asyncio.Event().wait()


class _Runtime:
    def __init__(
        self, actor_failure: BaseException | None, cleanup_failure: BaseException | None
    ) -> None:
        self.worker = self.dispatcher = _Actor(actor_failure)
        self.cleanup_failure = cleanup_failure
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")

    async def stop(self) -> None:
        self.calls.append("stop")
        if self.cleanup_failure is not None:
            raise self.cleanup_failure


async def _run_role(worker: bool, runtime: _Runtime, stop: asyncio.Event) -> None:
    if worker:
        await durable_worker_main.serve(cast(DurableWorkerRuntime, runtime), stop)
    else:
        await durable_dispatcher_main.serve(cast(DurableDispatcherRuntime, runtime), stop)


@pytest.mark.parametrize("worker", [True, False])
def test_actor_failure_only_keeps_exact_exception(worker: bool) -> None:
    primary = AssertionError("actor")
    runtime = _Runtime(primary, None)

    async def run() -> None:
        with pytest.raises(AssertionError) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is primary
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(observed.value)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_cleanup_failure_only_keeps_exact_exception(worker: bool) -> None:
    cleanup = RuntimeError("cleanup")
    runtime = _Runtime(None, cleanup)
    stop = asyncio.Event()
    stop.set()

    async def run() -> None:
        with pytest.raises(RuntimeError) as observed:
            await _run_role(worker, runtime, stop)
        assert observed.value is cleanup
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(observed.value)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_actor_primary_survives_ordinary_cleanup_failure(worker: bool) -> None:
    primary = AssertionError("actor")
    cleanup = RuntimeError("cleanup")
    independent = LookupError("independent cleanup cause")
    cleanup.__cause__ = independent
    runtime = _Runtime(primary, cleanup)

    async def run() -> None:
        with pytest.raises(AssertionError) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is primary
        assert _linked(primary, cleanup)
        assert cleanup.__cause__ is independent
        assert _linked(primary, independent)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(primary)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_cancellation_primary_survives_ordinary_cleanup_failure(worker: bool) -> None:
    cleanup = RuntimeError("cleanup")
    runtime = _Runtime(None, cleanup)

    async def run() -> None:
        task = asyncio.create_task(_run_role(worker, runtime, asyncio.Event()))
        await runtime.worker.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as observed:
            await task
        assert _linked(observed.value, cleanup)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(observed.value)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_fatal_cleanup_takes_precedence_over_actor_failure(worker: bool) -> None:
    primary = AssertionError("actor")
    cleanup = SystemExit("fatal cleanup")
    runtime = _Runtime(primary, cleanup)

    async def run() -> None:
        with pytest.raises(SystemExit) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is cleanup
        assert _linked(cleanup, primary)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(cleanup)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_fatal_cleanup_takes_precedence_over_cancellation(
    worker: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = asyncio.CancelledError("stop")
    cleanup = SystemExit("fatal cleanup")
    runtime = _Runtime(None, cleanup)

    async def cancelled_wait(*_args: object, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(asyncio, "wait", cancelled_wait)

    async def run() -> None:
        with pytest.raises(SystemExit) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is cleanup
        assert _linked(cleanup, primary)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(cleanup)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_reobserving_same_actor_exception_creates_no_self_link(worker: bool) -> None:
    primary = AssertionError("actor and cleanup")
    runtime = _Runtime(primary, primary)

    async def run() -> None:
        with pytest.raises(AssertionError) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is primary
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(primary)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_cleanup_group_retains_independent_failure_without_cycle(worker: bool) -> None:
    primary = AssertionError("actor")
    independent = RuntimeError("database close")
    cleanup = ExceptionGroup("runtime stop", [primary, independent])
    runtime = _Runtime(primary, cleanup)

    async def run() -> None:
        with pytest.raises(AssertionError) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is primary
        assert _linked(primary, independent)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(primary)

    asyncio.run(run())


class _ApiRuntime:
    def __init__(self, cleanup_failure: BaseException) -> None:
        self.admission = object()
        self.started = False
        self.cleanup_failure = cleanup_failure
        self.calls: list[str] = []

    async def start(self) -> None:
        self.calls.append("start")
        self.started = True

    async def stop(self) -> None:
        self.calls.append("stop")
        self.started = False
        raise self.cleanup_failure


@pytest.mark.parametrize("fatal", [False, True])
def test_api_lifespan_preserves_primary_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, fatal: bool
) -> None:
    monkeypatch.setenv("SUPER7_DURABLE_API_ENABLED", "1")
    primary = AssertionError("request lifespan")
    cleanup: BaseException = SystemExit("fatal cleanup") if fatal else RuntimeError("cleanup")
    runtime = _ApiRuntime(cleanup)
    app = durable_api_main.create_app(settings=Settings(), runtime=cast(DurableApiRuntime, runtime))

    async def run() -> None:
        with pytest.raises(SystemExit if fatal else AssertionError) as observed:
            async with app.router.lifespan_context(app):
                raise primary
        assert observed.value is (cleanup if fatal else primary)
        assert _linked(observed.value, primary)
        assert _linked(observed.value, cleanup)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(observed.value)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_existing_primary_cause_and_context_remain_accessible(worker: bool) -> None:
    primary = AssertionError("actor")
    prior_cause = KeyError("earlier cause")
    prior_context = ValueError("earlier context")
    primary.__cause__ = prior_cause
    primary.__context__ = prior_context
    cleanup = RuntimeError("cleanup")
    runtime = _Runtime(primary, cleanup)

    async def run() -> None:
        with pytest.raises(AssertionError) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is primary
        for error in (prior_cause, prior_context, cleanup):
            assert _linked(primary, error)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(primary)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_fatal_cleanup_retains_its_independent_cause(worker: bool) -> None:
    primary = AssertionError("actor")
    cleanup = SystemExit("fatal cleanup")
    prior_cause = LookupError("earlier cleanup cause")
    cleanup.__cause__ = prior_cause
    runtime = _Runtime(primary, cleanup)

    async def run() -> None:
        with pytest.raises(SystemExit) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is cleanup
        assert _linked(cleanup, primary)
        assert _linked(cleanup, prior_cause)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(cleanup)

    asyncio.run(run())


@pytest.mark.parametrize("worker", [True, False])
def test_exact_cancelled_primary_survives_ordinary_cleanup(
    worker: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = asyncio.CancelledError("stop")
    cleanup = RuntimeError("cleanup")
    runtime = _Runtime(None, cleanup)

    async def cancelled_wait(*_args: object, **_kwargs: object) -> None:
        raise primary

    monkeypatch.setattr(asyncio, "wait", cancelled_wait)

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError) as observed:
            await _run_role(worker, runtime, asyncio.Event())
        assert observed.value is primary
        assert _linked(primary, cleanup)
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(primary)

    asyncio.run(run())


def test_api_lifespan_cleanup_failure_without_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPER7_DURABLE_API_ENABLED", "1")
    cleanup = RuntimeError("cleanup")
    runtime = _ApiRuntime(cleanup)
    app = durable_api_main.create_app(settings=Settings(), runtime=cast(DurableApiRuntime, runtime))

    async def run() -> None:
        with pytest.raises(RuntimeError) as observed:
            async with app.router.lifespan_context(app):
                pass
        assert observed.value is cleanup
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(cleanup)

    asyncio.run(run())


class _NaturallyNestedRuntime(_Runtime):
    def __init__(self, primary: BaseException, mode: str) -> None:
        super().__init__(primary, None)
        self.actor_primary = primary
        self.mode = mode
        self.admission = object()
        self.inner = ValueError("inner cleanup failure")
        self.cleanup: BaseException = (
            SystemExit("fatal cleanup") if mode == "fatal" else RuntimeError("cleanup")
        )
        self.group_member = LookupError("nested group member")

    async def stop(self) -> None:
        self.calls.append("stop")
        try:
            raise self.inner
        except ValueError:
            if self.mode == "group":
                try:
                    raise self.group_member
                except LookupError:
                    raise ExceptionGroup(
                        "cleanup group",
                        [
                            ExceptionGroup(
                                "nested",
                                [
                                    cast(Exception, self.actor_primary),
                                    cast(Exception, self.cleanup),
                                    self.group_member,
                                ],
                            )
                        ],
                    ) from self.inner
            raise self.cleanup from self.inner


@pytest.mark.parametrize("target", ["worker", "dispatcher", "api"])
@pytest.mark.parametrize("mode", ["ordinary", "fatal", "group"])
def test_naturally_nested_cleanup_links_have_no_primary_cycle(
    target: str, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = AssertionError("active primary")
    runtime = _NaturallyNestedRuntime(primary, mode)

    async def run() -> None:
        if target == "api":
            monkeypatch.setenv("SUPER7_DURABLE_API_ENABLED", "1")
            app = durable_api_main.create_app(
                settings=Settings(), runtime=cast(DurableApiRuntime, runtime)
            )
            with pytest.raises(SystemExit if mode == "fatal" else AssertionError) as observed:
                async with app.router.lifespan_context(app):
                    raise primary
        else:
            with pytest.raises(SystemExit if mode == "fatal" else AssertionError) as observed:
                await _run_role(target == "worker", runtime, asyncio.Event())

        raised = observed.value
        assert raised is (runtime.cleanup if mode == "fatal" else primary)
        for failure in (primary, runtime.inner, runtime.cleanup):
            assert _linked(raised, failure)
        if mode == "group":
            assert _linked(raised, runtime.group_member)
        elif mode == "ordinary":
            assert runtime.cleanup.__cause__ is runtime.inner
        if mode == "fatal":
            assert runtime.inner.__context__ is primary
        assert runtime.calls == ["start", "stop"]
        _assert_no_cycle(raised)

    asyncio.run(run())
