"""Shared durable /analyze admission route, without analysis-process imports."""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.routing import APIRoute

from adapters.psycopg_analysis_job_repository import AnalysisJobPersistenceUnavailable
from core.exceptions import AnalysisError
from schemas.analysis import AnalyzeQueuedResponse, AnalyzeRequest
from services.callback_service import CallbackService
from services.durable_local_admission import (
    DurableAdmissionConflict,
    DurableAdmissionFull,
    DurableAdmissionInvalidKey,
    DurableAdmissionUnauthorized,
    DurableLocalAdmission,
)
from services.video_path_resolver import VideoPathResolver


def create_durable_admission_router(
    admission: DurableLocalAdmission,
    path_resolver: VideoPathResolver,
    callback_service: CallbackService,
) -> APIRouter:
    """Keep authentication ahead of body parsing and all existing admission mappings."""

    class AuthorizedRoute(APIRoute):
        def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
            handle = super().get_route_handler()

            async def authenticate_first(request: Request) -> Response:
                try:
                    admission.authenticate(request.headers.getlist("authorization"))
                except DurableAdmissionUnauthorized as error:
                    raise HTTPException(status_code=401, detail={"error": str(error)}) from None
                return await handle(request)

            return authenticate_first

    router = APIRouter(route_class=AuthorizedRoute)

    @router.post(
        "/analyze",
        response_model=AnalyzeQueuedResponse,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def analyze(payload: AnalyzeRequest, request: Request) -> AnalyzeQueuedResponse:
        try:
            idempotency_key = admission.key(request.headers.getlist("idempotency-key"))
        except DurableAdmissionInvalidKey as error:
            raise HTTPException(status_code=422, detail={"error": str(error)}) from None
        try:
            path_resolver.validate_reference(payload.video_url)
            callback_service.validate_callback_url(payload.callback_url)
        except (OSError, ValueError, AnalysisError) as error:
            raise HTTPException(status_code=422, detail={"error": str(error)}) from error
        try:
            outcome = await admission.admit(
                key=idempotency_key,
                video_id=payload.video_id,
                player_id=payload.player_id,
                video_reference=payload.video_url,
                callback_url=payload.callback_url,
            )
        except DurableAdmissionConflict as error:
            raise HTTPException(status_code=409, detail={"error": str(error)}) from None
        except DurableAdmissionFull as error:
            raise HTTPException(status_code=503, detail={"error": str(error)}) from None
        except AnalysisJobPersistenceUnavailable:
            raise HTTPException(
                status_code=503,
                detail={"error": "Analysis admission is temporarily unavailable."},
            ) from None
        return AnalyzeQueuedResponse(
            analysis_id=str(outcome.job.job_id),
            video_id=outcome.job.video_id,
            player_id=outcome.job.player_id,
        )

    return router
