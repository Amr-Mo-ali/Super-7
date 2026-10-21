"""Database-free controls for the actual Windows local launcher helper."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
import uvicorn

from scripts import local_durable_uvicorn


def test_local_launcher_uses_selector_before_server_serve(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed: list[tuple[str, object]] = []

    class FakeServer:
        started = False
        force_exit = False
        should_exit = False

        def __init__(self, config: Any) -> None:
            assert config.app == "durable_local_main:create_app"
            assert config.factory is True
            assert config.host == "127.0.0.1"
            assert config.port == 8123
            assert config.workers == 1
            assert config.reload is False

        async def serve(self) -> None:
            observed.append(("serve", asyncio.get_running_loop()))
            self.started = True

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert local_durable_uvicorn.main(["--port", "8123"]) == 0
    assert len(observed) == 1
    if sys.platform == "win32":
        assert isinstance(observed[0][1], asyncio.SelectorEventLoop)
    assert "local_durable_event_loop=" in capsys.readouterr().out


def test_local_launcher_check_loop_requires_no_app_or_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert local_durable_uvicorn.main(["--check-loop"]) == 0
    reported = capsys.readouterr().out.strip()
    if sys.platform == "win32":
        assert reported == "local_durable_event_loop=_WindowsSelectorEventLoop"
    else:
        assert reported.startswith("local_durable_event_loop=")


def test_local_launcher_treats_graceful_ctrl_c_as_normal_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeServer:
        started = True
        force_exit = False
        should_exit = True

        def __init__(self, config: Any) -> None:
            del config

        async def serve(self) -> None:
            assert asyncio.get_running_loop().is_running()
            raise KeyboardInterrupt

    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    assert local_durable_uvicorn.main([]) == 0
