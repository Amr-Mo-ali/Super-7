"""Explicit opt-in, single-slot durable worker process entry point."""

from __future__ import annotations

import asyncio
import signal
import sys
from os import environ

from core.config import Settings
from core.database_config import DatabaseSettings
from core.logging import configure_logging, get_logger
from services.cleanup_exception_links import remove_primary_backlinks
from services.durable_worker_composition import (
    WorkerConfiguration,
    parse_coordinator_timing,
    parse_worker_database_timeouts,
    parse_worker_loop_configuration,
)
from services.durable_worker_runtime import DurableWorkerRuntime


def _required(name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required for durable worker mode.")
    return value


def create_runtime() -> DurableWorkerRuntime:
    if environ.get("SUPER7_DURABLE_WORKER_ENABLED") != "1":
        raise RuntimeError("Durable worker role is not enabled.")
    settings = Settings.from_environment()
    timing = parse_coordinator_timing(_required)
    database = DatabaseSettings.from_mapping(environ)
    recovery_batch_size, worker_idle_seconds, model_catalog = parse_worker_loop_configuration(
        settings, _required
    )
    operation_timeout_ms, lock_timeout_ms = parse_worker_database_timeouts(_required)
    worker_config = WorkerConfiguration(
        timing,
        recovery_batch_size,
        worker_idle_seconds,
        model_catalog,
        operation_timeout_ms,
        lock_timeout_ms,
    )
    return DurableWorkerRuntime(
        settings=settings,
        database_settings=database,
        timing=worker_config.timing,
        recovery_batch_size=worker_config.recovery_batch_size,
        worker_idle_seconds=worker_config.idle_seconds,
        model_catalog=worker_config.model_catalog,
        operation_timeout_ms=worker_config.operation_timeout_ms,
        lock_timeout_ms=worker_config.lock_timeout_ms,
    )


async def serve(runtime: DurableWorkerRuntime, stop: asyncio.Event) -> None:
    """Expose actor failure as process failure; stop without cancelling child work."""
    await runtime.start()
    get_logger("football_analysis.durable_worker").info("durable_worker_ready")
    actor = asyncio.create_task(runtime.worker.wait())
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({actor, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if actor in done:
            await actor
            if not stop.is_set():
                raise RuntimeError("Durable worker stopped unexpectedly.")
    finally:
        primary = sys.exception()
        stopping.cancel()
        if not actor.done():
            actor.cancel()
        await asyncio.gather(stopping, actor, return_exceptions=True)
        cleanup_error: BaseException | None = None
        try:
            await runtime.stop()
        except BaseException as error:
            cleanup_error = error
        if cleanup_error is not None:
            if primary is None:
                raise cleanup_error
            if isinstance(cleanup_error, Exception):
                cleanup_error = remove_primary_backlinks(cleanup_error, primary)
            if cleanup_error is not primary:
                if isinstance(cleanup_error, Exception):
                    previous_cause = primary.__cause__
                    if previous_cause is not None and previous_cause is not cleanup_error:
                        cleanup_error = BaseExceptionGroup(
                            "Primary and cleanup causes.", [previous_cause, cleanup_error]
                        )
                    raise primary from cleanup_error
                previous_cause = cleanup_error.__cause__
                if previous_cause is not None and previous_cause is not primary:
                    raise cleanup_error from BaseExceptionGroup(
                        "Fatal cleanup and primary causes.", [previous_cause, primary]
                    )
                raise cleanup_error from primary


def main() -> int:
    configure_logging()
    runtime = create_runtime()

    async def run() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        old_handlers = {kind: signal.getsignal(kind) for kind in (signal.SIGINT, signal.SIGTERM)}
        try:
            for kind in old_handlers:
                signal.signal(kind, lambda *_: loop.call_soon_threadsafe(stop.set))
            await serve(runtime, stop)
        finally:
            for kind, handler in old_handlers.items():
                signal.signal(kind, handler)

    asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop if sys.platform == "win32" else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
