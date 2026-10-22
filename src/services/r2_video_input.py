"""Opt-in, worker-owned R2 staging for filename-only durable jobs."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from math import isfinite
from os import environ
from pathlib import Path
from tempfile import gettempdir
from time import monotonic
from typing import Protocol
from urllib.parse import urlsplit

import boto3  # type: ignore[import-untyped]
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.config import Settings
from domain.video_integrity import VideoIntegrity
from services.process_analysis_pool import ValidatedChildSuccess
from services.process_contracts import (
    ChildAnalysisRequest,
    ChildFailureKind,
    ParentCancelled,
    ParentFailure,
)
from services.video_path_resolver import VideoPathResolver

_BUCKET = "4goats-media-dev"
_PREFIX = "videos/"
_ACCOUNT_HOST = re.compile(r"[0-9a-f]{32}\.r2\.cloudflarestorage\.com")


class R2DownloadError(RuntimeError):
    """Sanitized transfer failure; SDK exceptions may contain private keys."""


class R2VideoUnavailable(R2DownloadError):
    """Typed missing-object evidence for durable terminal failure."""


class R2InvalidVideo(R2DownloadError):
    """A complete R2 object cannot match the requested video input."""


@dataclass(frozen=True, slots=True)
class R2VideoSettings:
    endpoint: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    temp_root: Path
    max_bytes: int
    timeout_seconds: float

    def __post_init__(self) -> None:
        url = urlsplit(self.endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or not _ACCOUNT_HOST.fullmatch(url.hostname)
            or url.username
            or url.password
            or url.port
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("R2 endpoint must be an account-scoped HTTPS API endpoint.")
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("Dedicated R2 read-only credentials are required.")
        if self.max_bytes <= 0 or not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("R2 transfer bounds must be positive.")


def configured_r2_video(settings: Settings) -> R2VideoSettings | None:
    """Only an explicit worker source selection loads dedicated read credentials."""
    source = environ.get("SUPER7_VIDEO_SOURCE", "local")
    if source == "local":
        return None
    if source != "r2":
        raise ValueError("SUPER7_VIDEO_SOURCE must be local or r2.")
    return R2VideoSettings(
        endpoint=environ.get("SUPER7_R2_ENDPOINT", ""),
        access_key_id=environ.get("SUPER7_R2_ACCESS_KEY_ID", ""),
        secret_access_key=environ.get("SUPER7_R2_SECRET_ACCESS_KEY", ""),
        temp_root=Path(environ.get("SUPER7_R2_TEMP_ROOT", gettempdir())).resolve(),
        max_bytes=settings.max_upload_bytes,
        timeout_seconds=settings.download_timeout_seconds,
    )


class _ChildExecutor(Protocol):
    async def execute_enveloped(
        self, request: ChildAnalysisRequest
    ) -> ValidatedChildSuccess | ParentFailure | ParentCancelled: ...


class R2VideoExecutor:
    """Keep a unique staged file until its submitted child has settled."""

    abort_on_lease_loss = True
    verifies_video_integrity = True

    def __init__(self, child: _ChildExecutor, settings: R2VideoSettings) -> None:
        self._child = child
        self._settings = settings

    async def execute_enveloped(
        self,
        request: ChildAnalysisRequest,
        *,
        before_child: Callable[[], Awaitable[None]] | None = None,
    ) -> ValidatedChildSuccess | ParentFailure | ParentCancelled:
        VideoPathResolver._validate_filename(request.video_reference)
        self._settings.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="super7-r2-", dir=self._settings.temp_root
        ) as directory:
            destination = Path(directory) / request.video_reference
            transfer = asyncio.create_task(
                asyncio.to_thread(
                    self._download, request.video_reference, destination, request.video_integrity
                )
            )
            try:
                await asyncio.shield(transfer)
            except asyncio.CancelledError:
                await _settle_after_cancellation(transfer)
                raise
            except R2VideoUnavailable:
                return ParentFailure(
                    "VideoNotFoundError",
                    "Analysis could not be completed.",
                    ChildFailureKind.VIDEO_UNAVAILABLE,
                )
            except R2InvalidVideo:
                return ParentFailure(
                    "InvalidVideoError",
                    "Analysis could not be completed.",
                    ChildFailureKind.VIDEO_INVALID,
                )
            if before_child is not None:
                await before_child()
            staged = ChildAnalysisRequest(
                request.analysis_id,
                request.video_id,
                request.player_id,
                request.video_reference,
                analysis_version=request.analysis_version,
                staged_video_path=str(destination),
                video_integrity=request.video_integrity,
            )
            # The coordinator tracks this task. A lost lease leaves it outstanding;
            # its directory is removed only after the submitted child returns.
            child = asyncio.create_task(self._child.execute_enveloped(staged))
            try:
                return await asyncio.shield(child)
            except asyncio.CancelledError:
                await _settle_after_cancellation(child)
                raise

    def _download(self, filename: str, destination: Path, integrity: VideoIntegrity | None) -> None:
        settings = self._settings
        if shutil.disk_usage(destination.parent).free < settings.max_bytes:
            raise R2DownloadError("Insufficient temporary disk capacity for R2 video.")
        key = _PREFIX + filename
        deadline = monotonic() + settings.timeout_seconds
        client = None
        try:
            client = boto3.client(
                "s3",
                endpoint_url=settings.endpoint,
                region_name="auto",
                aws_access_key_id=settings.access_key_id,
                aws_secret_access_key=settings.secret_access_key,
                config=Config(
                    connect_timeout=5,
                    read_timeout=5,
                    retries={"total_max_attempts": 1},
                    s3={"addressing_style": "virtual"},
                ),
            )
            response = client.get_object(Bucket=_BUCKET, Key=key)
            body = response["Body"]
            try:
                length = response.get("ContentLength")
                if type(length) is not int or length < 0 or length > settings.max_bytes:
                    raise R2DownloadError("R2 object size is unavailable or exceeds the limit.")
                received = 0
                digest = hashlib.sha256() if integrity is not None else None
                with destination.open("xb") as output:
                    while True:
                        if monotonic() >= deadline:
                            raise R2DownloadError("R2 transfer exceeded its time limit.")
                        chunk = body.read(min(1024 * 1024, settings.max_bytes - received + 1))
                        if not chunk:
                            break
                        received += len(chunk)
                        if received > settings.max_bytes:
                            raise R2DownloadError("R2 object exceeds the byte limit.")
                        if digest is not None:
                            digest.update(chunk)
                        output.write(chunk)
                if received != length:
                    raise R2DownloadError("R2 transfer ended before its declared length.")
                if received == 0:
                    raise R2InvalidVideo("Requested R2 video is empty.")
                if integrity is not None and (
                    received != integrity.size_bytes
                    or digest is None
                    or not hmac.compare_digest(digest.hexdigest(), integrity.sha256)
                ):
                    raise R2InvalidVideo("R2 video integrity check failed.")
            finally:
                body.close()
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                raise R2VideoUnavailable("Requested R2 video is unavailable.") from None
            raise R2DownloadError("R2 video download failed.") from None
        except R2DownloadError:
            raise
        except Exception:
            raise R2DownloadError("R2 video download failed.") from None
        finally:
            if client is not None:
                primary = sys.exception()
                try:
                    client.close()
                except Exception:
                    if primary is None:
                        raise R2DownloadError("R2 connection cleanup failed.") from None


async def _settle_after_cancellation(task: asyncio.Task[object]) -> None:
    """Keep the staged directory owned until blocking I/O or the child settles."""
    while True:
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return
        except Exception:
            return
        else:
            return
