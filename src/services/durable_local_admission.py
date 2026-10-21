"""Opt-in Apex authentication and idempotent durable route admission."""

from __future__ import annotations

from hmac import compare_digest
from uuid import uuid4

from pydantic import HttpUrl

from domain.analysis_job import (
    AnalysisJobAccepted,
    AnalysisJobCapacityRejected,
    AnalysisJobExisting,
    AnalysisJobIdempotencyConflict,
)
from services.analysis_job_admission import (
    AnalysisJobAdmissionRepository,
    IdempotencyLookupProtector,
    admit_analysis_job,
)


class DurableAdmissionUnauthorized(ValueError):
    """The supplied bearer credential is absent or invalid."""


class DurableAdmissionInvalidKey(ValueError):
    """The Apex key header is absent or invalid."""


class DurableAdmissionConflict(ValueError):
    """One key is already bound to another immutable request."""


class DurableAdmissionFull(RuntimeError):
    """The durable queued capacity is exhausted."""


class DurableLocalAdmission:
    """Map one configured Apex credential to a rotation-stable caller scope."""

    def __init__(
        self,
        *,
        repository: AnalysisJobAdmissionRepository,
        protector: IdempotencyLookupProtector,
        bearer_secret: str,
        analysis_version: str,
        attempt_limit: int,
        max_queue_size: int,
    ) -> None:
        if type(bearer_secret) is not str or not bearer_secret:
            raise ValueError("Apex bearer secret must be configured.")
        self._repository = repository
        self._protector = protector
        self._bearer_secret = bearer_secret.encode("utf-8")
        self._version = analysis_version
        self._attempt_limit = attempt_limit
        self._capacity = max_queue_size

    def authenticate(self, values: list[str]) -> None:
        if len(values) != 1:
            raise DurableAdmissionUnauthorized("Apex authentication required.")
        scheme, separator, credential = values[0].partition(" ")
        if (
            separator != " "
            or scheme.lower() != "bearer"
            or not compare_digest(credential.encode("utf-8"), self._bearer_secret)
        ):
            raise DurableAdmissionUnauthorized("Apex authentication required.")

    @staticmethod
    def key(values: list[str]) -> str:
        if len(values) != 1:
            raise DurableAdmissionInvalidKey("Idempotency-Key is required.")
        key = values[0]
        if not key or key != key.strip() or len(key) > 255 or any(ord(c) < 32 for c in key):
            raise DurableAdmissionInvalidKey("Idempotency-Key is invalid.")
        return key

    async def admit(
        self,
        *,
        key: str,
        video_id: str,
        player_id: str,
        video_reference: str,
        callback_url: HttpUrl,
    ) -> AnalysisJobAccepted | AnalysisJobExisting:
        outcome = await admit_analysis_job(
            repository=self._repository,
            protector=self._protector,
            job_id=uuid4(),
            caller_scope="apex",
            raw_idempotency_key=key,
            video_id=video_id,
            player_id=player_id,
            video_reference=video_reference,
            callback_url=callback_url,
            analysis_version=self._version,
            attempt_limit=self._attempt_limit,
            max_queue_size=self._capacity,
        )
        if isinstance(outcome, AnalysisJobIdempotencyConflict):
            raise DurableAdmissionConflict("Idempotency-Key conflicts with an existing request.")
        if isinstance(outcome, AnalysisJobCapacityRejected):
            raise DurableAdmissionFull("Analysis queue is full.")
        return outcome
