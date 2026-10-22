"""Server-only HTTPS callback admission and transport boundaries."""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import httpx
import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import HttpUrl

import durable_api_main
from core.config import Settings
from services.callback_service import CallbackService
from services.durable_api_runtime import DurableApiRuntime
from services.durable_callback_https_policy import SETTING, https_only_from_mapping
from services.durable_callback_transport import create_callback_transport
from services.video_path_resolver import VideoPathResolver

ROOT = Path(__file__).resolve().parents[1]


def _answers(_host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


class _Admission:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def authenticate(self, values: list[str]) -> None:
        assert values == ["Bearer synthetic-secret"]

    def key(self, values: list[str]) -> str:
        assert values == ["synthetic-key"]
        return values[0]

    async def admit(self, **kwargs: object) -> SimpleNamespace:
        callback_url = kwargs["callback_url"]
        assert isinstance(callback_url, HttpUrl)
        self.urls.append(str(callback_url))
        return SimpleNamespace(
            job=SimpleNamespace(
                job_id=uuid4(), video_id=kwargs["video_id"], player_id=kwargs["player_id"]
            )
        )


class _Runtime:
    def __init__(self) -> None:
        self.admission = _Admission()
        self.started = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    async def database_readiness(self) -> SimpleNamespace:
        return SimpleNamespace(ready=True)


class _Path:
    def validate_reference(self, reference: str) -> None:
        assert reference == "synthetic.mp4"


def test_server_admission_rejects_http_before_job_write(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SUPER7_DURABLE_API_ENABLED", "1")
    monkeypatch.setenv(SETTING, "1")
    runtime = _Runtime()
    resolved: list[str] = []

    def resolver(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        resolved.append(host)
        return _answers(host, port, **kwargs)

    validator = CallbackService(1, logging.getLogger("https.policy.test"), resolver=resolver)
    app = durable_api_main.create_app(
        settings=Settings(),
        runtime=cast(DurableApiRuntime, runtime),
        path_resolver=cast(VideoPathResolver, _Path()),
        callback_service=validator,
    )
    signed_query = "sig=synthetic%2Btoken%3D&part=1"

    async def request(callback_url: str) -> httpx.Response:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://testserver"
            ) as client:
                return await client.post(
                    "/analyze",
                    headers={
                        "Authorization": "Bearer synthetic-secret",
                        "Idempotency-Key": "synthetic-key",
                    },
                    json={
                        "videoId": "synthetic-video",
                        "playerId": "synthetic-player",
                        "videoUrl": "synthetic.mp4",
                        "callbackUrl": callback_url,
                    },
                )

    http_url = f"http://callback.example/hook?{signed_query}"
    rejected = asyncio.run(request(http_url))
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["error"] == "Callback URL must use HTTPS."
    assert runtime.admission.urls == [] and resolved == []
    assert signed_query not in caplog.text

    https_url = f"https://callback.example/hook?{signed_query}"
    accepted = asyncio.run(request(https_url))
    assert accepted.status_code == 202
    assert runtime.admission.urls == [https_url]
    assert resolved == ["callback.example"]


@pytest.mark.parametrize("status", [204, 302])
def test_server_delivery_preserves_signed_https_query_without_redirect(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[httpx.Request] = []
    options: list[dict[str, object]] = []
    original_client = httpx.AsyncClient

    def make_client(**kwargs: object) -> httpx.AsyncClient:
        options.append(kwargs)
        follow_redirects = kwargs["follow_redirects"]
        trust_env = kwargs["trust_env"]
        timeout = kwargs["timeout"]
        assert isinstance(follow_redirects, bool)
        assert isinstance(trust_env, bool)
        assert isinstance(timeout, (int, float))

        async def receive(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(status, headers={"Location": "http://other.example/"})

        return original_client(
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            timeout=timeout,
            transport=httpx.MockTransport(receive),
        )

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    validator = CallbackService(1, logging.getLogger("https.policy.test"), resolver=_answers)
    transport = create_callback_transport(validator, https_only_callbacks=True)
    url = "https://callback.example:8443/hook?sig=synthetic%2Btoken%3D&part=1"

    async def send() -> int:
        return await transport(url, b"{}", {"X-Test": "synthetic"}, 2)

    with patch("services.durable_callback_transport.httpx.AsyncClient", make_client):
        result = asyncio.run(send())
    assert result == status
    assert options == [{"follow_redirects": False, "trust_env": False, "timeout": 2}]
    assert len(captured) == 1
    request = captured[0]
    assert request.url.host == "8.8.8.8"
    assert request.url.raw_path == b"/hook?sig=synthetic%2Btoken%3D&part=1"
    assert request.headers["Host"] == "callback.example:8443"
    assert request.extensions["sni_hostname"] == "callback.example"


def test_server_delivery_rejects_stored_http_before_resolution_or_client() -> None:
    resolved: list[str] = []

    def resolver(host: str, port: int, **kwargs: object) -> list[tuple[object, ...]]:
        resolved.append(host)
        return _answers(host, port, **kwargs)

    validator = CallbackService(1, logging.getLogger("https.policy.test"), resolver=resolver)
    transport = create_callback_transport(validator, https_only_callbacks=True)

    class ForbiddenClient:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("HTTP callback reached the network client")

    async def send() -> int:
        return await transport("http://callback.example/hook?sig=synthetic", b"{}", {}, 2)

    with patch("services.durable_callback_transport.httpx.AsyncClient", ForbiddenClient):
        with pytest.raises(OSError, match="destination unavailable"):
            asyncio.run(send())
    assert resolved == []


def test_https_policy_setting_is_explicit_and_defaults_off() -> None:
    assert https_only_from_mapping({}) is False
    assert https_only_from_mapping({SETTING: "0"}) is False
    assert https_only_from_mapping({SETTING: "1"}) is True
    with pytest.raises(ValueError, match=SETTING):
        https_only_from_mapping({SETTING: "true"})


def test_server_compose_forces_policy_on_both_roles() -> None:
    manifest = yaml.safe_load((ROOT / "docker-compose.server.yml").read_text(encoding="utf-8"))
    services = manifest["services"]
    assert services["api"]["environment"][SETTING] == "1"
    assert services["dispatcher"]["environment"][SETTING] == "1"
    assert SETTING not in services["worker"]["environment"]
    assert services["dispatcher"]["profiles"] == ["dispatch"]
