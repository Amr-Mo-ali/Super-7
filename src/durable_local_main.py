"""Explicit local-only ASGI factory: uvicorn durable_local_main:create_app --factory."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from os import environ

import httpx
from fastapi import FastAPI
from pydantic import HttpUrl

from core.config import Settings
from core.database_config import DatabaseSettings
from core.logging import get_logger
from domain.analysis_job_attempt import LeaseDuration, RecoveryBatchSize
from main import create_app as compose_app
from services.analysis_callback_delivery import CallbackTransport
from services.analysis_job_coordinator import CoordinatorTiming
from services.callback_service import CallbackService
from services.durable_local_runtime import DurableLocalRuntime


def _required(name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required for local durable mode.")
    return value


def _transport(validator: CallbackService) -> CallbackTransport:
    async def post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        # The app never allows loopback through this transport. An isolated
        # receiver needs an explicitly injected test-only validator/transport.
        validator.validate_callback_url(HttpUrl(url))
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=timeout
            ) as client:
                response = await client.post(url, content=body, headers=headers)
            return response.status_code
        except httpx.TransportError as error:
            raise OSError("Callback transport unavailable.") from error

    return post


def create_app() -> FastAPI:
    """Fail closed unless explicitly opted into the separately configured role."""
    if environ.get("SUPER7_DURABLE_LOCAL_ENABLED") != "1":
        raise RuntimeError("Local durable mode is not enabled.")
    settings = Settings.from_environment()
    database = DatabaseSettings.from_mapping(environ)
    hmac_secret = bytes.fromhex(_required("SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX"))
    callback = CallbackService(
        settings.callback_timeout_seconds, get_logger("football_analysis.callback")
    )
    timing = CoordinatorTiming(
        LeaseDuration(timedelta(seconds=float(_required("SUPER7_DURABLE_LEASE_SECONDS")))),
        heartbeat_seconds=float(_required("SUPER7_DURABLE_HEARTBEAT_SECONDS")),
        operation_watchdog_seconds=float(_required("SUPER7_DURABLE_WATCHDOG_SECONDS")),
        jitter_seconds=float(_required("SUPER7_DURABLE_JITTER_SECONDS")),
        finalization_budget_seconds=float(_required("SUPER7_DURABLE_FINALIZE_SECONDS")),
        retry_delays_seconds=(
            float(_required("SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS")),
            float(_required("SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS")),
        ),
    )
    raw_model = f"{settings.model_path}+{settings.ball_model_path}+bytetrack"
    runtime = DurableLocalRuntime(
        settings=settings,
        database_settings=database,
        bearer_secret=_required("SUPER7_APEX_BEARER_SECRET"),
        idempotency_hmac_secret=hmac_secret,
        attempt_limit=int(_required("SUPER7_DURABLE_ATTEMPT_LIMIT")),
        timing=timing,
        recovery_batch_size=RecoveryBatchSize(int(_required("SUPER7_DURABLE_RECOVERY_BATCH"))),
        worker_idle_seconds=float(_required("SUPER7_DURABLE_WORKER_IDLE_SECONDS")),
        model_catalog={raw_model: _required("SUPER7_DURABLE_MODEL_ID")},
        callback_transport=_transport(callback),
        logger=get_logger("football_analysis.durable_local"),
        operation_timeout_ms=int(_required("SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS")),
        lock_timeout_ms=int(_required("SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS")),
    )
    return compose_app(settings, callback_service=callback, durable_runtime=runtime)
