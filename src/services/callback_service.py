"""Bounded, secure delivery of final analysis callbacks."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from ipaddress import ip_address
from socket import SOCK_STREAM, getaddrinfo
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, Field, HttpUrl

_RETRY_DELAYS: Final = (1.0, 2.0, 4.0)


class DetailedRatings(BaseModel):
    """Reserved detailed ratings; null means no validated score is available."""

    speed_and_fitness: float | None = Field(default=None, ge=0, le=100)
    ball_control_and_individual_skill: float | None = Field(default=None, ge=0, le=100)
    passing_and_playmaking: float | None = Field(default=None, ge=0, le=100)
    shooting_and_finishing: float | None = Field(default=None, ge=0, le=100)
    defending_and_duels: float | None = Field(default=None, ge=0, le=100)
    tactical_intelligence_and_teamwork: float | None = Field(default=None, ge=0, le=100)
    positioning_and_off_ball_movement: float | None = Field(default=None, ge=0, le=100)


class CallbackPayload(BaseModel):
    """Successful or non-failed analysis callback payload."""

    request_id: str
    video_id: str
    player_id: str
    status: str
    summary: dict[str, Any]
    ratings: dict[str, Any]
    overall: dict[str, Any] | None = None
    detailed: DetailedRatings
    events: dict[str, Any]
    error: dict[str, str] | None = None


class FailedCallbackPayload(BaseModel):
    """Failure callback payload, intentionally without detailed ratings."""

    request_id: str
    video_id: str
    player_id: str
    status: str
    summary: dict[str, Any]
    ratings: dict[str, Any]
    overall: dict[str, Any] | None = None
    events: dict[str, Any]
    error: dict[str, str] | None = None


class _RejectRedirects(HTTPRedirectHandler):
    """Prevent a validated callback endpoint from redirecting to a private target."""

    def redirect_request(self, *args: object, **kwargs: object) -> Request | None:
        del args, kwargs
        return None


class CallbackService:
    """Deliver result callbacks with retry, timeout, and non-fatal failure handling."""

    def __init__(
        self,
        timeout_seconds: float,
        logger: logging.Logger,
        transport: Callable[[str, bytes, float], int] | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = getaddrinfo,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._logger = logger
        self._transport = transport or self._post
        self._resolver = resolver
        self._sleep = sleep

    async def send_result(
        self, callback_url: HttpUrl, payload: CallbackPayload | FailedCallbackPayload
    ) -> bool:
        """Attempt delivery; callback failure is logged and never raised into analysis handling."""
        try:
            self._validate_url(callback_url)
        except (OSError, ValueError) as error:
            self._logger.warning(
                "callback_rejected request_id=%s reason=%s", payload.request_id, str(error)
            )
            return False
        body = json.dumps(payload.model_dump(mode="json"), separators=(",", ":")).encode()
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                response_status = await asyncio.to_thread(
                    self._transport, str(callback_url), body, self._timeout_seconds
                )
                if not 200 <= response_status < 300:
                    raise RuntimeError(f"callback returned HTTP {response_status}")
                self._logger.info(
                    "callback_delivered request_id=%s attempt=%s status=%s",
                    payload.request_id,
                    attempt + 1,
                    response_status,
                )
                return True
            except (HTTPError, OSError, RuntimeError, TimeoutError, URLError) as error:
                self._logger.warning(
                    "callback_delivery_failed request_id=%s attempt=%s error=%s",
                    payload.request_id,
                    attempt + 1,
                    str(error),
                )
                if attempt == len(_RETRY_DELAYS):
                    break
                await self._sleep(_RETRY_DELAYS[attempt])
        return False

    def validate_callback_url(self, callback_url: HttpUrl) -> None:
        """Validate a callback destination before a job is admitted to the queue."""
        self._validate_url(callback_url)

    def _validate_url(self, callback_url: HttpUrl) -> None:
        if callback_url.scheme not in {"http", "https"}:
            raise ValueError("callback URL must use HTTP or HTTPS")
        host = callback_url.host
        if host is None:
            raise ValueError("callback URL must include a host")
        port = callback_url.port or (443 if callback_url.scheme == "https" else 80)
        addresses = {str(item[4][0]) for item in self._resolver(host, port, type=SOCK_STREAM)}
        if not addresses or any(not ip_address(address).is_global for address in addresses):
            raise ValueError("callback URL must resolve only to public IP addresses")

    @staticmethod
    def _post(url: str, body: bytes, timeout: float) -> int:
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with build_opener(_RejectRedirects()).open(request, timeout=timeout) as response:
            return int(response.getcode())
