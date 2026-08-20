"""Focused lifecycle evidence for the production one-process-pool wiring."""

import asyncio
from pathlib import Path
from typing import cast

import httpx
from fastapi import FastAPI
from process_pool_lifecycle_fake import FakeProcessPoolLifecycle
from pydantic import HttpUrl

from core.config import Settings
from main import create_app
from schemas.analysis import Diagnostics, NonCompletedResponse
from services.analysis_queue import AnalysisJob, AnalysisJobState, AnalysisQueue
from services.callback_service import CallbackPayload, CallbackService, FailedCallbackPayload
from services.process_contracts import ChildAnalysisRequest
from services.video_path_resolver import VideoPathResolver


class _Resolver:
    def validate_reference(self, _: str) -> None:
        return None

    def validate_storage_root(self) -> Path:
        return Path(__file__).parent

    def storage_root_checks(self) -> dict[str, bool]:
        return {"exists": True, "readable": True, "accessible": True, "read_only": True}


class _Callback:
    def __init__(self) -> None:
        self.payloads: list[CallbackPayload | FailedCallbackPayload] = []

    async def send_result(
        self, _: HttpUrl, payload: CallbackPayload | FailedCallbackPayload
    ) -> bool:
        self.payloads.append(payload)
        return True

    def validate_callback_url(self, _: HttpUrl) -> None:
        return None


def _response(request: ChildAnalysisRequest) -> NonCompletedResponse:
    return NonCompletedResponse(
        analysis_id=request.analysis_id,
        status="no_players_detected",
        warnings=[],
        diagnostics=Diagnostics(0, 0, 0, 0, 0, 0),
    )


def _app(pool: FakeProcessPoolLifecycle, queue: AnalysisQueue, callback: _Callback) -> FastAPI:
    return create_app(
        Settings(),
        path_resolver=cast(VideoPathResolver, _Resolver()),
        analysis_queue=queue,
        callback_service=cast(CallbackService, callback),
        process_analysis_pool=pool,
    )


def test_process_pool_and_worker_construction_are_inert() -> None:
    pool = FakeProcessPoolLifecycle([_response])
    queue = AnalysisQueue(1)
    app = _app(pool, queue, _Callback())
    assert pool.start_calls == pool.shutdown_calls == 0
    assert pool.requests == []
    assert queue.metrics().worker_running is False
    assert app.state.process_analysis_pool is pool


def test_lifespan_starts_pool_before_prequeued_work() -> None:
    async def scenario() -> None:
        pool = FakeProcessPoolLifecycle([_response])
        queue = AnalysisQueue(1)
        callback = _Callback()
        job = AnalysisJob.create(
            "queued", "video", "player", "safe.mp4", HttpUrl("http://8.8.8.8/callback")
        )
        assert await queue.submit(job)
        app = _app(pool, queue, callback)
        async with app.router.lifespan_context(app):
            while not pool.requests:
                await asyncio.sleep(0)
            assert pool._events[:2] == ["pool.start", "pool.execute"]
            assert queue.metrics().worker_running is True
        assert pool.shutdown_calls == 1
        assert queue.state_of("queued") is AnalysisJobState.COMPLETED

    asyncio.run(scenario())


def test_runtime_uses_parent_callback_and_child_request_contract() -> None:
    async def scenario() -> None:
        pool = FakeProcessPoolLifecycle([_response])
        queue = AnalysisQueue(1)
        callback = _Callback()
        app = _app(pool, queue, callback)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.post("/analyze", json=_payload())
            analysis_id = response.json()["analysisId"]
            while not callback.payloads:
                await asyncio.sleep(0)
        assert response.status_code == 202
        assert pool.requests[0].analysis_id == analysis_id
        assert pool.requests[0].video_reference == "safe.mp4"
        assert not hasattr(pool.requests[0], "callback_url")
        assert callback.payloads[0].request_id == analysis_id
        assert queue.state_of(analysis_id) is AnalysisJobState.COMPLETED

    asyncio.run(scenario())


def _payload() -> dict[str, str]:
    return {
        "videoId": "video",
        "playerId": "player",
        "videoUrl": "safe.mp4",
        "callbackUrl": "http://8.8.8.8/callback",
    }
