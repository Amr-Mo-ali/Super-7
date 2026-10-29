"""Worker-side R2 staging without network, database, or callbacks."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.config import Config  # type: ignore[import-untyped]
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.config import Settings
from core.exceptions import VideoPathResolutionError
from domain.video_integrity import VideoIntegrity
from services.process_contracts import ChildAnalysisRequest, ChildFailureKind, ParentFailure
from services.r2_video_input import (
    R2DownloadError,
    R2VideoExecutor,
    R2VideoSettings,
    configured_r2_video,
)


class _Child:
    def __init__(self) -> None:
        self.paths: list[Path] = []
        self.bytes_seen: list[bytes] = []

    async def execute_enveloped(self, request: ChildAnalysisRequest) -> ParentFailure:
        assert request.staged_video_path is not None
        path = Path(request.staged_video_path)
        self.paths.append(path)
        self.bytes_seen.append(path.read_bytes())
        return ParentFailure("TestResult", "Analysis could not be completed.")


class _Client:
    def __init__(self, body: io.BytesIO, size: int) -> None:
        self.body = body
        self.size = size
        self.calls: list[dict[str, str]] = []
        self.closed = False

    def get_object(self, **kwargs: str) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"ContentLength": self.size, "Body": self.body}

    def close(self) -> None:
        self.closed = True


def _settings(root: Path, *, max_bytes: int = 100, timeout: float = 3) -> R2VideoSettings:
    return R2VideoSettings(
        "https://00000000000000000000000000000000.r2.cloudflarestorage.com",
        "read-only-id",
        "read-only-secret",
        root,
        max_bytes,
        timeout,
    )


def _request(filename: str = "clip.mp4") -> ChildAnalysisRequest:
    return ChildAnalysisRequest("job_1", "video_1", "player_1", filename, analysis_version="1.0.0")


def _verified_request(data: bytes, *, digest: str | None = None) -> ChildAnalysisRequest:
    return ChildAnalysisRequest(
        "job_1",
        "video_1",
        "player_1",
        "clip.mp4",
        analysis_version="1.0.0",
        video_integrity=VideoIntegrity(len(data), digest or hashlib.sha256(data).hexdigest()),
    )


@pytest.mark.parametrize("corruption", ("digest", "size", "truncated", "size_and_truncated"))
def test_verified_r2_rejects_corrupt_bytes_before_child_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    data = b"expected video"
    body = b"wrong bytes!!!" if corruption == "digest" else data
    if corruption == "size":
        body += b"!"
    declared = len(data) + 1 if corruption in ("size", "size_and_truncated") else len(data)
    if corruption == "truncated":
        body = data[:-1]
    monkeypatch.setattr(
        "services.r2_video_input.boto3.client",
        lambda *a, **k: _Client(io.BytesIO(body), declared),
    )
    child = _Child()
    operation = R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(
        _verified_request(data)
    )
    if corruption in ("truncated", "size_and_truncated"):
        with pytest.raises(R2DownloadError, match="declared length"):
            asyncio.run(operation)
    else:
        result = asyncio.run(operation)
        assert isinstance(result, ParentFailure)
        assert result.failure_kind is ChildFailureKind.VIDEO_INVALID
        assert result.public_message == "Analysis could not be completed."
    assert child.bytes_seen == []
    assert list(tmp_path.iterdir()) == []


def test_empty_r2_object_never_reaches_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client(io.BytesIO(b""), 0)
    monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: client)
    child = _Child()
    result = asyncio.run(R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_request()))
    assert isinstance(result, ParentFailure)
    assert result.failure_kind is ChildFailureKind.VIDEO_INVALID
    assert child.bytes_seen == []
    assert client.closed
    assert list(tmp_path.iterdir()) == []


def test_verified_r2_accepts_matching_digest_before_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"expected video"
    monkeypatch.setattr(
        "services.r2_video_input.boto3.client",
        lambda *a, **k: _Client(io.BytesIO(data), len(data)),
    )
    child = _Child()
    asyncio.run(
        R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_verified_request(data))
    )
    assert child.bytes_seen == [data]
    assert list(tmp_path.iterdir()) == []


def test_source_selection_defaults_local_and_requires_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUPER7_VIDEO_SOURCE", raising=False)
    assert configured_r2_video(Settings()) is None
    monkeypatch.setenv("SUPER7_VIDEO_SOURCE", "r2")
    monkeypatch.delenv("SUPER7_R2_ENDPOINT", raising=False)
    monkeypatch.delenv("SUPER7_R2_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("SUPER7_R2_SECRET_ACCESS_KEY", raising=False)
    with pytest.raises(ValueError, match="endpoint"):
        configured_r2_video(Settings())


def test_r2_download_uses_fixed_bucket_prefix_and_cleans_after_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _Client(io.BytesIO(b"video bytes"), 11)
    seen: dict[str, object] = {}

    def factory(*args: object, **kwargs: object) -> _Client:
        seen.update(kwargs)
        return client

    monkeypatch.setattr("services.r2_video_input.boto3.client", factory)
    child = _Child()
    result = asyncio.run(R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_request()))
    assert isinstance(result, ParentFailure)
    assert child.bytes_seen == [b"video bytes"]
    assert client.closed
    assert not child.paths[0].exists()
    assert client.calls == [{"Bucket": "4goats-media-dev", "Key": "videos/clip.mp4"}]
    assert seen["region_name"] == "auto"
    assert (
        seen["endpoint_url"] == "https://00000000000000000000000000000000.r2.cloudflarestorage.com"
    )
    config = seen["config"]
    assert isinstance(config, Config)
    assert config.s3["addressing_style"] == "virtual"


@pytest.mark.parametrize(
    "filename",
    [
        "https://media.example/x.mp4",
        "../x.mp4",
        "a/b.mp4",
        "C:\\x.mp4",
        "C:clip.mp4",
        "clip?x.mp4",
        "bad.txt",
    ],
)
def test_r2_rejects_urls_paths_and_unsupported_filenames(tmp_path: Path, filename: str) -> None:
    child = _Child()
    with pytest.raises((ValueError, VideoPathResolutionError)):
        asyncio.run(
            R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_request(filename))
        )
    assert not child.paths


def test_r2_size_failure_never_submits_child_and_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.r2_video_input.boto3.client",
        lambda *args, **kwargs: _Client(io.BytesIO(b"oversize"), 8),
    )
    child = _Child()
    with pytest.raises(R2DownloadError, match="limit"):
        asyncio.run(
            R2VideoExecutor(child, _settings(tmp_path, max_bytes=4)).execute_enveloped(_request())
        )
    assert not child.paths
    assert list(tmp_path.iterdir()) == []


def test_missing_object_returns_typed_unavailable_without_key_or_sdk_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Missing:
        def get_object(self, **kwargs: str) -> None:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "private/videos/clip.mp4"}},
                "GetObject",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: Missing())
    result = asyncio.run(
        R2VideoExecutor(_Child(), _settings(tmp_path)).execute_enveloped(_request())
    )
    assert isinstance(result, ParentFailure)
    assert result.failure_kind is ChildFailureKind.VIDEO_UNAVAILABLE
    assert "private" not in str(result)
    assert list(tmp_path.iterdir()) == []


def test_access_failure_remains_operational(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Denied:
        def get_object(self, **kwargs: str) -> None:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "private credential detail"}},
                "GetObject",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: Denied())
    child = _Child()
    with pytest.raises(R2DownloadError, match="download failed") as captured:
        asyncio.run(R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_request()))
    assert "private" not in str(captured.value)
    assert child.bytes_seen == []
    assert list(tmp_path.iterdir()) == []


def test_r2_failure_log_uses_only_allowlisted_sdk_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    request: pytest.FixtureRequest,
) -> None:
    logger = logging.getLogger("football_analysis.r2_video_input")
    logger.addHandler(caplog.handler)
    request.addfinalizer(lambda: logger.removeHandler(caplog.handler))
    secret = "token-signed-url-and-private-key"

    class Denied:
        def get_object(self, **kwargs: str) -> None:
            raise ClientError(
                {
                    "Error": {"Code": "AccessDenied", "Message": secret, "Key": secret},
                    "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": secret},
                },
                "GetObject",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: Denied())
    with pytest.raises(R2DownloadError, match="download failed"):
        asyncio.run(
            R2VideoExecutor(_Child(), _settings(tmp_path)).execute_enveloped(
                _request(), attempt_number=2
            )
        )
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "r2_transfer_failed analysis_id=job_1 attempt=2 phase=get-object"
        " error_type=ClientError http_status=403 s3_code=AccessDenied" in message
        for message in messages
    )
    assert all(secret not in message and "clip.mp4" not in message for message in messages)


def test_r2_failure_log_rejects_untrusted_status_and_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    request: pytest.FixtureRequest,
) -> None:
    logger = logging.getLogger("football_analysis.r2_video_input")
    logger.addHandler(caplog.handler)
    request.addfinalizer(lambda: logger.removeHandler(caplog.handler))
    secret = "Bearer synthetic-secret"

    class Denied:
        def get_object(self, **kwargs: str) -> None:
            raise ClientError(
                {
                    "Error": {"Code": secret, "Message": secret},
                    "ResponseMetadata": {"HTTPStatusCode": "403"},
                },
                "GetObject",
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr("services.r2_video_input.boto3.client", lambda *a, **k: Denied())
    with pytest.raises(R2DownloadError, match="download failed"):
        asyncio.run(R2VideoExecutor(_Child(), _settings(tmp_path)).execute_enveloped(_request()))
    messages = [record.getMessage() for record in caplog.records]
    assert any("http_status=unavailable s3_code=unavailable" in message for message in messages)
    assert all(secret not in message for message in messages)


def test_disk_capacity_failure_remains_operational_before_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.r2_video_input.shutil.disk_usage", lambda _: SimpleNamespace(free=0)
    )
    child = _Child()
    with pytest.raises(R2DownloadError, match="disk capacity"):
        asyncio.run(R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_request()))
    assert child.bytes_seen == []
    assert list(tmp_path.iterdir()) == []


def test_post_download_lease_guard_prevents_child_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.r2_video_input.boto3.client",
        lambda *a, **k: _Client(io.BytesIO(b"video"), 5),
    )
    child = _Child()
    called = False

    async def lost_lease() -> None:
        nonlocal called
        called = True
        raise RuntimeError("lease lost")

    with pytest.raises(RuntimeError, match="lease lost"):
        asyncio.run(
            R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(
                _request(), before_child=lost_lease
            )
        )
    assert called
    assert not child.paths
    assert list(tmp_path.iterdir()) == []


def test_cancelled_download_waits_for_blocked_io_then_cleans_without_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BlockingBody(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            entered.set()
            assert release.wait(2)
            return super().read(size)

    monkeypatch.setattr(
        "services.r2_video_input.boto3.client",
        lambda *a, **k: _Client(BlockingBody(b"video"), 5),
    )
    child = _Child()

    async def scenario() -> None:
        task = asyncio.create_task(
            R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_request())
        )
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()  # A second lease/shutdown cancellation must still wait for I/O.
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert not child.paths
    assert list(tmp_path.iterdir()) == []


def test_transfer_deadline_stops_after_blocking_read_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowBody(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            time.sleep(0.02)
            return super().read(size)

    monkeypatch.setattr(
        "services.r2_video_input.boto3.client",
        lambda *a, **k: _Client(SlowBody(b"video"), 5),
    )
    child = _Child()
    with pytest.raises(R2DownloadError, match="time limit"):
        asyncio.run(
            R2VideoExecutor(child, _settings(tmp_path, timeout=0.001)).execute_enveloped(_request())
        )
    assert not child.paths
    assert list(tmp_path.iterdir()) == []


def test_cancelled_child_keeps_staged_file_until_child_settles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "services.r2_video_input.boto3.client",
        lambda *a, **k: _Client(io.BytesIO(b"video"), 5),
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    class WaitingChild:
        path: Path | None = None

        async def execute_enveloped(self, request: ChildAnalysisRequest) -> ParentFailure:
            assert request.staged_video_path is not None
            self.path = Path(request.staged_video_path)
            entered.set()
            await release.wait()
            assert self.path.read_bytes() == b"video"
            return ParentFailure("TestResult", "Analysis could not be completed.")

    child = WaitingChild()

    async def scenario() -> None:
        task = asyncio.create_task(
            R2VideoExecutor(child, _settings(tmp_path)).execute_enveloped(_request())
        )
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert child.path is not None and child.path.exists()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert child.path is not None and not child.path.exists()
    assert list(tmp_path.iterdir()) == []
