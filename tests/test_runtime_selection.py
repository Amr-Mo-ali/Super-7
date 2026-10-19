"""Runtime selection cannot silently expose the in-memory admission path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
import yaml  # type: ignore[import-untyped]

import durable_dispatcher_main
import main
from api.routes import create_router
from core.config import Settings
from core.database_config import DatabaseSettings
from services.analysis_queue import AnalysisQueue
from services.callback_service import CallbackService
from services.durable_local_runtime import DurableLocalRuntime
from services.video_path_resolver import VideoPathResolver

ROOT = Path(__file__).resolve().parents[1]
FLAG = "SUPER7_LEGACY_IN_MEMORY_ENABLED"


def _isolated_python(script: str, *, enabled: bool) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop(FLAG, None)
    if enabled:
        environment[FLAG] = "1"
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", script],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_default_import_has_no_legacy_route_or_actors() -> None:
    result = _isolated_python(
        """
import asyncio
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))
assert 'SUPER7_LEGACY_IN_MEMORY_ENABLED' not in os.environ
import main
import durable_local_main
import durable_api_main
import durable_worker_main
import durable_dispatcher_main
assert '/analyze' not in main.app.openapi()['paths']
assert not hasattr(main.app.state, 'analysis_queue')
try:
    main.create_app()
except RuntimeError as error:
    assert 'SUPER7_LEGACY_IN_MEMORY_ENABLED=1' in str(error)
else:
    raise AssertionError('legacy factory started without opt-in')
async def start():
    async with main.app.router.lifespan_context(main.app):
        pass
try:
    asyncio.run(start())
except RuntimeError as error:
    assert 'main:app is disabled' in str(error)
else:
    raise AssertionError('default ASGI app started')
""",
        enabled=False,
    )
    assert result.returncode == 0, result.stderr


def test_explicit_legacy_opt_in_exposes_original_route() -> None:
    result = _isolated_python(
        """
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd() / 'src'))
assert os.environ['SUPER7_LEGACY_IN_MEMORY_ENABLED'] == '1'
import main
assert '/analyze' in main.app.openapi()['paths']
assert main.app.state.analysis_queue.metrics().worker_running is False
""",
        enabled=True,
    )
    assert result.returncode == 0, result.stderr


def test_direct_memory_router_requires_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    with pytest.raises(RuntimeError, match="Legacy in-memory admission is disabled"):
        create_router(
            logging.getLogger("test.runtime_selection"),
            cast(VideoPathResolver, object()),
            cast(CallbackService, object()),
            AnalysisQueue(1),
        )


class _Path:
    def validate_storage_root(self) -> Path:
        return ROOT


class _DurableRuntime:
    def __init__(self) -> None:
        self.admission = object()
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False


def test_combined_durable_composition_starts_without_legacy_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    runtime = _DurableRuntime()
    app = main.create_app(
        Settings(),
        path_resolver=cast(VideoPathResolver, _Path()),
        durable_runtime=cast(DurableLocalRuntime, runtime),
    )

    async def run() -> None:
        async with app.router.lifespan_context(app):
            assert runtime.started
            assert not app.state.analysis_queue.metrics().worker_running

    asyncio.run(run())
    assert not runtime.started
    assert "/analyze" in app.openapi()["paths"]


def test_dispatcher_factory_does_not_require_legacy_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(FLAG, raising=False)
    monkeypatch.setenv("SUPER7_DURABLE_DISPATCHER_ENABLED", "1")
    monkeypatch.setattr(
        DatabaseSettings,
        "from_mapping",
        classmethod(lambda cls, values: object()),
    )
    runtime = object()
    monkeypatch.setattr(
        durable_dispatcher_main, "DurableDispatcherRuntime", lambda **kwargs: runtime
    )
    assert durable_dispatcher_main.create_runtime() is runtime


def test_image_and_compose_commands_fail_closed_or_select_test_role() -> None:
    for image in ("Dockerfile", "Dockerfile.durable-trial"):
        source = (ROOT / image).read_text(encoding="utf-8")
        command = json.loads(source.splitlines()[-1].removeprefix("CMD "))
        assert command[:2] == ["python", "-c"]
        result = subprocess.run(
            [sys.executable, *command[1:]], text=True, capture_output=True, check=False
        )
        assert result.returncode != 0
        assert "Explicit runtime role required" in result.stderr

    root = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    assert "command" not in root["services"]["football-analysis"]
    integration = yaml.safe_load(
        (ROOT / "docker-compose.integration.yml").read_text(encoding="utf-8")
    )
    test_service = integration["services"]["super-7"]
    assert test_service["command"][:2] == ["uvicorn", "main:app"]
    assert test_service["environment"][FLAG] == "1"

    trial = yaml.safe_load((ROOT / "docker-compose.durable-trial.yml").read_text(encoding="utf-8"))[
        "services"
    ]
    assert trial["durable-api"]["command"][:2] == [
        "uvicorn",
        "durable_api_main:create_app",
    ]
    assert trial["durable-worker"]["command"] == ["python", "-m", "durable_worker_main"]
    assert trial["durable-dispatcher"]["command"] == [
        "python",
        "-m",
        "durable_dispatcher_main",
    ]
