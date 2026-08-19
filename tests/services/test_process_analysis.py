"""Deterministic spawn-boundary coverage for the unused MVP-2A foundation."""

import asyncio
import pickle

import pytest

from services.process_analysis import (
    PROCESS_ANALYSIS_SCHEMA_VERSION,
    ProcessAnalysisCancelled,
    ProcessAnalysisError,
    ProcessAnalysisFailure,
    ProcessAnalysisReady,
    ProcessAnalysisRequest,
    ProcessAnalysisState,
    ProcessAnalysisSuccess,
    ProcessAnalysisSupervisor,
    ProcessModelMetadata,
    ProcessRuntimeConfig,
    RequestQueue,
    ResponseQueue,
)


def _success_child(requests: RequestQueue, responses: ResponseQueue) -> None:
    responses.put(ProcessAnalysisReady())
    request = requests.get()
    if request is not None:
        responses.put(
            ProcessAnalysisSuccess(
                request.analysis_id, '{"fixture":true}', 1, ProcessModelMetadata("fake-model")
            )
        )


def _wrong_id_child(requests: RequestQueue, responses: ResponseQueue) -> None:
    responses.put(ProcessAnalysisReady())
    request = requests.get()
    if request is not None:
        responses.put(ProcessAnalysisCancelled("stale"))


def _bad_handshake_child(requests: RequestQueue, responses: ResponseQueue) -> None:
    responses.put(ProcessAnalysisFailure("startup", "STARTUP", "safe", 0))
    requests.get()


def _silent_child(requests: RequestQueue, responses: ResponseQueue) -> None:
    responses.put(ProcessAnalysisReady())
    requests.get()
    requests.get()


def _request() -> ProcessAnalysisRequest:
    return ProcessAnalysisRequest(
        "analysis-1",
        "video-1",
        "player-1",
        "safe/video.mp4",
        ProcessRuntimeConfig("/videos", "debug", 1024, "1.0.0"),
    )


def test_contracts_are_pickle_safe_and_validate_their_invariants() -> None:
    request = _request()
    success = ProcessAnalysisSuccess("analysis-1", "{}", 0, ProcessModelMetadata("fixture"))
    failure = ProcessAnalysisFailure("analysis-1", "SAFE", "Analysis failed.", 0)
    cancelled = ProcessAnalysisCancelled("analysis-1")
    assert pickle.loads(pickle.dumps(request)) == request
    assert pickle.loads(pickle.dumps(success)) == success
    assert pickle.loads(pickle.dumps(failure)) == failure
    assert pickle.loads(pickle.dumps(cancelled)) == cancelled
    with pytest.raises(ValueError, match="relative"):
        ProcessAnalysisRequest("id", "video", "player", "/unsafe.mp4", request.runtime_config)
    with pytest.raises(ValueError, match="negative"):
        ProcessAnalysisFailure("id", "SAFE", "safe", -1)


def test_spawned_supervisor_accepts_one_matching_result_and_shuts_down() -> None:
    async def scenario() -> None:
        supervisor = ProcessAnalysisSupervisor(_success_child, response_timeout_seconds=2)
        await supervisor.start()
        assert supervisor.state is ProcessAnalysisState.READY
        assert supervisor.pid is not None
        result = await supervisor.submit(_request())
        assert isinstance(result, ProcessAnalysisSuccess)
        assert result.result_json == '{"fixture":true}'
        assert supervisor.state is ProcessAnalysisState.READY
        await supervisor.shutdown()
        await supervisor.shutdown()
        assert supervisor.state is ProcessAnalysisState.STOPPED

    asyncio.run(scenario())


def test_wrong_analysis_id_is_rejected_and_busy_state_is_cleared() -> None:
    async def scenario() -> None:
        supervisor = ProcessAnalysisSupervisor(_wrong_id_child, response_timeout_seconds=2)
        try:
            await supervisor.start()
            with pytest.raises(ProcessAnalysisError, match="stale"):
                await supervisor.submit(_request())
            assert supervisor.state is ProcessAnalysisState.FAILED
        finally:
            await supervisor.shutdown()

    asyncio.run(scenario())


def test_invalid_readiness_handshake_fails_without_leaking_a_child() -> None:
    async def scenario() -> None:
        supervisor = ProcessAnalysisSupervisor(_bad_handshake_child, startup_timeout_seconds=2)
        with pytest.raises(ProcessAnalysisError, match="handshake"):
            await supervisor.start()
        assert supervisor.state is ProcessAnalysisState.STOPPED
        assert supervisor.pid is None

    asyncio.run(scenario())


def test_cancelling_submit_restores_ready_state_and_shutdown_remains_bounded() -> None:
    async def scenario() -> None:
        supervisor = ProcessAnalysisSupervisor(_silent_child, response_timeout_seconds=2)
        try:
            await supervisor.start()
            pending = asyncio.create_task(supervisor.submit(_request()))
            for _ in range(20):
                if supervisor.state is ProcessAnalysisState.BUSY:
                    break
                await asyncio.sleep(0)
            assert supervisor.state is ProcessAnalysisState.BUSY
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert supervisor.state is ProcessAnalysisState.READY
        finally:
            await supervisor.shutdown()
        assert supervisor.state is ProcessAnalysisState.STOPPED

    asyncio.run(scenario())


def test_schema_version_is_explicit() -> None:
    assert PROCESS_ANALYSIS_SCHEMA_VERSION == _request().schema_version
