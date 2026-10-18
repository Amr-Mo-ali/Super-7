"""The supported in-process processor still receives its explicit analysis graph."""

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from pydantic import HttpUrl

import api.routes as routes
from api.request_lifecycle import RequestLifecycle
from core.config import Settings
from schemas.analysis import Diagnostics, NonCompletedResponse
from services.analysis_composition import create_analysis_components
from services.analysis_queue import AnalysisJob, AnalysisJobState
from services.callback_service import CallbackPayload, CallbackService, FailedCallbackPayload
from services.video_path_resolver import VideoPathResolver


class _Lifecycle:
    async def execute_with_artifacts(
        self, analysis_id: str, operation: Callable[[object, object], NonCompletedResponse]
    ) -> NonCompletedResponse:
        assert analysis_id == "analysis-id"
        return operation(object(), object())


class _Resolver:
    def resolve(self, reference: str) -> Path:
        assert reference == "safe.mp4"
        return Path("safe.mp4")


class _Callback:
    def __init__(self) -> None:
        self.payloads: list[CallbackPayload | FailedCallbackPayload] = []

    async def send_result(
        self, _: HttpUrl, payload: CallbackPayload | FailedCallbackPayload
    ) -> bool:
        self.payloads.append(payload)
        return True


def test_in_process_processor_passes_analysis_components_to_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings()
    components = create_analysis_components(
        settings,
        player_detector_logger=logging.getLogger("test.player"),
        ball_detector_logger=logging.getLogger("test.ball"),
    )
    seen: list[tuple[object, ...]] = []

    def analyze(*args: object) -> NonCompletedResponse:
        seen.append(args)
        return NonCompletedResponse(
            analysis_id="analysis-id",
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

    monkeypatch.setattr(routes, "_analyze_uploaded", analyze)
    callback = _Callback()
    processor = routes.create_analysis_job_processor(
        settings,
        components.validator,
        components.tracker,
        components.selector,
        components.extractor,
        components.ball_proximity_analyzer,
        components.movement_analyzer,
        components.interaction_analyzer,
        components.technical_event_analyzer,
        components.pass_detector,
        components.shot_detector,
        logging.getLogger("test.analysis"),
        components.physical_scorer,
        cast(RequestLifecycle, _Lifecycle()),
        cast(VideoPathResolver, _Resolver()),
        cast(CallbackService, callback),
    )
    job = AnalysisJob.create(
        "analysis-id", "video", "player", "safe.mp4", HttpUrl("http://8.8.8.8/callback")
    )

    async def run() -> AnalysisJobState:
        return await processor(job)

    assert asyncio.run(run()) is AnalysisJobState.COMPLETED
    assert len(seen) == 1
    assert seen[0][:12] == (
        settings,
        components.validator,
        components.tracker,
        components.selector,
        components.extractor,
        components.ball_proximity_analyzer,
        components.movement_analyzer,
        components.interaction_analyzer,
        components.technical_event_analyzer,
        components.pass_detector,
        components.shot_detector,
        logging.getLogger("test.analysis"),
    )
    assert seen[0][12] is components.physical_scorer
    assert len(callback.payloads) == 1
    assert callback.payloads[0].request_id == "analysis-id"
    assert callback.payloads[0].status == "no_players_detected"
