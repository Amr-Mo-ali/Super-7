"""Pure, shared legacy-compatible callback projection; no transport."""

from __future__ import annotations

import json
from uuid import UUID

from api.public_rating_mapper import event_arbitration, public_rating_v2
from schemas.analysis import AnalyzeResponse, CompletedResponse
from services.callback_service import CallbackPayload, DetailedRatings, FailedCallbackPayload
from services.detailed_rating.engine import DetailedRatingEngine

_FAILURE_MESSAGE = "Analysis could not be completed."


def callback_payload(video_id: str, player_id: str, result: AnalyzeResponse) -> CallbackPayload:
    """Produce the exact present-day callback body from a response and job IDs."""
    if isinstance(result, CompletedResponse) and result.result_availability == "UNAVAILABLE":
        return CallbackPayload(
            request_id=result.analysis_id,
            video_id=video_id,
            player_id=player_id,
            status="COMPLETED",
            summary={},
            ratings={},
            overall=None,
            detailed=DetailedRatings(),
            events={},
            resultAvailability="UNAVAILABLE",
            unavailabilityReason=result.unavailability_reason,
            player=None,
            overallConfidence=None,
        )
    if isinstance(result, CompletedResponse):
        assert result.selected_player is not None
        assert result.scores is not None
    arbitration = event_arbitration(result) if isinstance(result, CompletedResponse) else None
    public_result = public_rating_v2(result, arbitration)
    serialized = public_result.model_dump(mode="json")
    detailed = DetailedRatings()
    if isinstance(result, CompletedResponse):
        assert result.selected_player is not None
        assert result.scores is not None
        detailed = DetailedRatingEngine().evaluate(
            result.scores.physical,
            result.technical_event_analysis,
            result.diagnostics.technical_event_analysis_quality,
            result.pass_detection,
            result.shot_detection,
            arbitration,
            result.selected_player.track_id,
        )
    return CallbackPayload(
        request_id=result.analysis_id,
        video_id=video_id,
        player_id=player_id,
        status="COMPLETED" if isinstance(result, CompletedResponse) else result.status,
        summary=serialized.get("summary", {}),
        ratings=serialized.get("ratings", {}),
        overall=serialized.get("overall"),
        detailed=detailed,
        events=serialized.get("events", {}),
        **(
            {
                "result_availability": "AVAILABLE",
                "unavailability_reason": None,
                "player": serialized["player"],
                "overall_confidence": result.player_rating_summary.overall.confidence,
            }
            if isinstance(result, CompletedResponse) and result.player_rating_summary is not None
            else {}
        ),
    )


def failure_callback_snapshot_v1(
    *, job_id: UUID, video_id: str, player_id: str, safe_code: str
) -> bytes:
    """Versioned v1 failure mapping, derived from locked job identity and terminal code."""
    payload = FailedCallbackPayload(
        request_id=str(job_id),
        video_id=video_id,
        player_id=player_id,
        status="failed",
        summary={},
        ratings={},
        events={},
        error={"code": safe_code, "message": _FAILURE_MESSAGE},
    )
    return json.dumps(
        payload.model_dump(mode="json", by_alias=True), separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
