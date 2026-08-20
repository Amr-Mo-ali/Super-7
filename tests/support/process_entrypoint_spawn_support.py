"""Spawn-importable fake analysis support used only by process-entrypoint tests."""

from __future__ import annotations

import os

from core.config import Settings
from schemas.analysis import Diagnostics, NonCompletedResponse
from services import process_entrypoint
from services.process_entrypoint import ChildAnalysisRequest, ChildAnalysisResult


def initialize_fake_analysis_child(settings: Settings) -> None:
    """Initialize production child state, then install a deterministic calculation fake."""
    process_entrypoint.initialize_analysis_child(settings)
    # Install the spawned-child fake without adding a production test seam.
    vars(process_entrypoint)["_analyze_uploaded"] = _fake_analyze_uploaded


def run_fake_child_analysis(request: ChildAnalysisRequest) -> tuple[int, ChildAnalysisResult]:
    """Return safe child identity alongside the production child result envelope."""
    return os.getpid(), process_entrypoint.run_child_analysis(request)


def _fake_analyze_uploaded(*args: object) -> NonCompletedResponse:
    return NonCompletedResponse(
        analysis_id=str(args[13]),
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
