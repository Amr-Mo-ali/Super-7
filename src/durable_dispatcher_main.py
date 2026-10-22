"""Explicit opt-in, single-actor durable callback dispatcher process entry point."""

from __future__ import annotations

import asyncio
import signal
import sys
from os import environ

from core.database_config import DatabaseSettings
from core.logging import configure_logging, get_logger
from services.callback_service import CallbackService
from services.cleanup_exception_links import remove_primary_backlinks
from services.durable_callback_https_policy import https_only_from_mapping
from services.durable_callback_transport import create_callback_transport
from services.durable_dispatcher_runtime import DurableDispatcherRuntime


def create_runtime() -> DurableDispatcherRuntime:
    if environ.get("SUPER7_DURABLE_DISPATCHER_ENABLED") != "1":
        raise RuntimeError("Durable dispatcher role is not enabled.")
    https_only_callbacks = https_only_from_mapping(environ)
    logger = get_logger("football_analysis.durable_dispatcher")
    validator = CallbackService(10, logger)
    return DurableDispatcherRuntime(
        database_settings=DatabaseSettings.from_mapping(environ),
        callback_transport=create_callback_transport(
            validator, https_only_callbacks=https_only_callbacks
        ),
    )


async def serve(runtime: DurableDispatcherRuntime, stop: asyncio.Event) -> None:
    """Expose actor failure as process failure; finish delivery before DB close."""
    await runtime.start()
    get_logger("football_analysis.durable_dispatcher").info("durable_dispatcher_ready")
    actor = asyncio.create_task(runtime.dispatcher.wait())
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({actor, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if actor in done:
            await actor
            if not stop.is_set():
                raise RuntimeError("Durable dispatcher stopped unexpectedly.")
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
