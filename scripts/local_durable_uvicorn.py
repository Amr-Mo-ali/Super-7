"""Foreground Uvicorn launcher with a Windows Selector loop for Psycopg async."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Coroutine
from typing import Any

import uvicorn


def _run_with_compatible_loop(coroutine: Coroutine[Any, Any, None]) -> None:
    # The loop must exist before Uvicorn starts the ASGI lifespan/DB pool.
    if sys.platform == "win32":
        asyncio.run(coroutine, loop_factory=asyncio.SelectorEventLoop)
    else:
        asyncio.run(coroutine)


def _report_running_loop() -> None:
    loop = asyncio.get_running_loop()
    if sys.platform == "win32" and not isinstance(loop, asyncio.SelectorEventLoop):
        raise RuntimeError("Local durable PostgreSQL requires a Windows Selector event loop.")
    print(f"local_durable_event_loop={type(loop).__name__}", flush=True)


async def _serve(server: uvicorn.Server) -> None:
    _report_running_loop()
    await server.serve()


async def _check_loop() -> None:
    _report_running_loop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check-loop", action="store_true")
    args = parser.parse_args(argv)
    if args.check_loop:
        _run_with_compatible_loop(_check_loop())
        return 0
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    config = uvicorn.Config(
        "durable_local_main:create_app",
        host="127.0.0.1",
        port=args.port,
        factory=True,
        workers=1,
        reload=False,
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    try:
        _run_with_compatible_loop(_serve(server))
    except KeyboardInterrupt:
        # Uvicorn re-raises a captured Ctrl+C after its graceful shutdown.
        if not server.should_exit:
            raise
    return 0 if server.started and not server.force_exit else 1


if __name__ == "__main__":
    raise SystemExit(main())
