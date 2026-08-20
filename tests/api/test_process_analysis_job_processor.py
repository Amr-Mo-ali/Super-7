"""Focused parent-side tests for the unused process-backed job processor."""

import asyncio
import logging

import pytest
from pydantic import HttpUrl, TypeAdapter

from api.routes import create_process_analysis_job_processor
from schemas.analysis import Diagnostics, NonCompletedResponse
from services.analysis_queue import AnalysisJob, AnalysisJobState
from services.callback_service import CallbackPayload, FailedCallbackPayload
from services.process_contracts import (
    ChildAnalysisRequest,
    ParentCancelled,
    ParentChildResult,
    ParentFailure,
)


class FakeProcessPool:
    def __init__(self, outcome: ParentChildResult | Exception) -> None:
        self.outcome = outcome
        self.requests: list[ChildAnalysisRequest] = []

    async def execute(self, request: ChildAnalysisRequest) -> ParentChildResult:
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeCallbackService:
    def __init__(self, outcome: bool | Exception = True) -> None:
        self.outcome = outcome
        self.payloads: list[CallbackPayload | FailedCallbackPayload] = []

    async def send_result(
        self, callback_url: HttpUrl, payload: CallbackPayload | FailedCallbackPayload
    ) -> bool:
        del callback_url
        self.payloads.append(payload)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _job() -> AnalysisJob:
    return AnalysisJob.create(
        "analysis-1",
        "video-1",
        "player-1",
        "safe.mp4",
        TypeAdapter(HttpUrl).validate_python("http://72.62.28.146/callback-secret"),
    )


def _response() -> NonCompletedResponse:
    return NonCompletedResponse(
        analysis_id="analysis-1",
        status="no_players_detected",
        warnings=[],
        diagnostics=Diagnostics(
            frames_processed=0,
            frames_with_player_detections=0,
            total_person_detections=0,
            tracks_created=0,
            valid_candidate_tracks=0,
            ball_detections=0,
        ),
    )


def test_success_converts_the_exact_child_request_and_sends_parent_callback() -> None:
    async def scenario() -> None:
        pool = FakeProcessPool(_response())
        callback = FakeCallbackService()
        state = await create_process_analysis_job_processor(
            pool, callback, logging.getLogger("test.process.processor")
        )(_job())
        assert state is AnalysisJobState.COMPLETED
        assert callback.payloads[0].status == "no_players_detected"
        assert isinstance(callback.payloads[0], CallbackPayload)
        request = pool.requests[0]
        assert request.analysis_id == "analysis-1"
        assert request.video_id == "video-1"
        assert request.player_id == "player-1"
        assert request.video_reference == "safe.mp4"

    asyncio.run(scenario())


@pytest.mark.parametrize("callback_outcome", [False, RuntimeError("secret-callback-marker")])
def test_success_callback_delivery_failure_keeps_completed(
    callback_outcome: bool | Exception,
) -> None:
    async def scenario() -> None:
        callback = FakeCallbackService(callback_outcome)
        state = await create_process_analysis_job_processor(
            FakeProcessPool(_response()), callback, logging.getLogger("test.process.processor")
        )(_job())
        assert state is AnalysisJobState.COMPLETED
        assert len(callback.payloads) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("callback_outcome", [False, RuntimeError("secret-callback-marker")])
def test_parent_failure_sends_sanitized_failure_callback_and_keeps_failed(
    callback_outcome: bool | Exception,
) -> None:
    async def scenario() -> None:
        callback = FakeCallbackService(callback_outcome)
        state = await create_process_analysis_job_processor(
            FakeProcessPool(ParentFailure("AnalysisBoom", "Analysis could not be completed.")),
            callback,
            logging.getLogger("test.process.processor"),
        )(_job())
        assert state is AnalysisJobState.FAILED
        assert callback.payloads[0].error == {
            "code": "AnalysisBoom",
            "message": "Analysis could not be completed.",
        }
        assert isinstance(callback.payloads[0], FailedCallbackPayload)

    asyncio.run(scenario())


def test_parent_cancellation_sends_no_callback() -> None:
    async def scenario() -> None:
        callback = FakeCallbackService()
        state = await create_process_analysis_job_processor(
            FakeProcessPool(ParentCancelled("analysis-1")),
            callback,
            logging.getLogger("test.process.processor"),
        )(_job())
        assert state is AnalysisJobState.CANCELLED
        assert callback.payloads == []

    asyncio.run(scenario())


def test_unexpected_pool_error_is_sanitized_in_callback_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        caplog.set_level(logging.INFO, logger="test.process.processor")
        callback = FakeCallbackService()
        state = await create_process_analysis_job_processor(
            FakeProcessPool(RuntimeError("secret-pool-marker C:/secret/video.mp4")),
            callback,
            logging.getLogger("test.process.processor"),
        )(_job())
        assert state is AnalysisJobState.FAILED
        assert callback.payloads[0].error == {
            "code": "ProcessPoolError",
            "message": "Analysis could not be completed.",
        }

    asyncio.run(scenario())
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "analysis_execution_finished" in messages
    assert "error_type=RuntimeError" in messages
    assert "secret-pool-marker" not in messages
    assert "C:/secret/video.mp4" not in messages
    assert "72.62.28.146" not in messages
