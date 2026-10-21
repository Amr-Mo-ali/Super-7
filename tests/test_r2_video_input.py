"""Worker-side R2 staging without network, database, or callbacks."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError  # type: ignore[import-untyped]

from core.config import Settings
from core.exceptions import VideoPathResolutionError
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
        "https://account.r2.cloudflarestorage.com",
        "read-only-id",
        "read-only-secret",
        root,
        max_bytes,
        timeout,
    )


def _request(filename: str = "clip.mp4") -> ChildAnalysisRequest:
    return ChildAnalysisRequest("job_1", "video_1", "player_1", filename, analysis_version="1.0.0")


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
    assert seen["endpoint_url"] == "https://account.r2.cloudflarestorage.com"


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
