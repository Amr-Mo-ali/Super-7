"""Explicit local-only ASGI factory: uvicorn durable_local_main:create_app --factory."""

from __future__ import annotations

from os import environ

import httpx as httpx
from fastapi import FastAPI

from core.config import Settings
from core.database_config import DatabaseSettings
from core.logging import get_logger
from main import create_app as compose_app
from services.analysis_callback_delivery import CallbackTransport
from services.callback_service import CallbackService
from services.durable_callback_transport import create_callback_transport
from services.durable_local_runtime import DurableLocalRuntime
from services.durable_worker_composition import (
    WorkerConfiguration,
    parse_coordinator_timing,
    parse_worker_database_timeouts,
    parse_worker_loop_configuration,
)


def _required(name: str) -> str:
    value = environ.get(name)
    if value is None or not value.strip():
        raise ValueError(f"{name} is required for local durable mode.")
    return value


def _transport(validator: CallbackService) -> CallbackTransport:
    """Compatibility hook for the existing network-free transport controls."""
    return create_callback_transport(validator)


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
    timing = parse_coordinator_timing(_required)
    bearer_secret = _required("SUPER7_APEX_BEARER_SECRET")
    attempt_limit = int(_required("SUPER7_DURABLE_ATTEMPT_LIMIT"))
    recovery_batch_size, worker_idle_seconds, model_catalog = parse_worker_loop_configuration(
        settings, _required
    )
    transport = _transport(callback)
    logger = get_logger("football_analysis.durable_local")
    operation_timeout_ms, lock_timeout_ms = parse_worker_database_timeouts(_required)
    worker_config = WorkerConfiguration(
        timing,
        recovery_batch_size,
        worker_idle_seconds,
        model_catalog,
        operation_timeout_ms,
        lock_timeout_ms,
    )
    runtime = DurableLocalRuntime(
        settings=settings,
        database_settings=database,
        bearer_secret=bearer_secret,
        idempotency_hmac_secret=hmac_secret,
        attempt_limit=attempt_limit,
        timing=worker_config.timing,
        recovery_batch_size=worker_config.recovery_batch_size,
        worker_idle_seconds=worker_config.idle_seconds,
        model_catalog=worker_config.model_catalog,
        callback_transport=transport,
        logger=logger,
        operation_timeout_ms=worker_config.operation_timeout_ms,
        lock_timeout_ms=worker_config.lock_timeout_ms,
    )
    return compose_app(settings, callback_service=callback, durable_runtime=runtime)
