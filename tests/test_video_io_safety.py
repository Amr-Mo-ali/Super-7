"""Deterministic security and cleanup tests for public video URL downloads."""

from email.message import Message

import pytest
from pydantic import HttpUrl, TypeAdapter

from core.config import Settings
from core.exceptions import DownloadError, DownloadTimeoutError, UploadTooLargeError
from services.video_downloader import VideoDownloader


class FakeResponse:
    def __init__(self, chunks: list[bytes], content_type: str = "video/mp4") -> None:
        self._chunks = iter(chunks)
        self.headers = Message()
        self.headers.set_type(content_type)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        del amount
        return next(self._chunks, b"")

    def getcode(self) -> int:
        return 200


def _url(value: str = "https://cdn.example.com/video.mp4") -> HttpUrl:
    return TypeAdapter(HttpUrl).validate_python(value)


def _public_resolver(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
    del host
    return [(None, None, None, None, ("8.8.8.8", port))]


def test_valid_url_streams_to_a_temporary_file_and_cleans_up() -> None:
    downloader = VideoDownloader(
        Settings(), lambda _, __: FakeResponse([b"video-bytes"]), _public_resolver
    )
    with downloader.download(_url()) as path:
        saved_path = path
        assert path.read_bytes() == b"video-bytes"
    assert not saved_path.exists()


def test_download_timeout_is_translated_to_a_request_error() -> None:
    def timeout(*_: object) -> FakeResponse:
        raise TimeoutError

    downloader = VideoDownloader(Settings(), timeout, _public_resolver)
    with pytest.raises(DownloadTimeoutError):
        with downloader.download(_url()):
            raise AssertionError("timeout entered context")


def test_download_enforces_the_size_limit() -> None:
    downloader = VideoDownloader(
        Settings(max_upload_bytes=4), lambda _, __: FakeResponse([b"12345"]), _public_resolver
    )
    with pytest.raises(UploadTooLargeError):
        with downloader.download(_url()):
            raise AssertionError("oversized download entered context")


def test_download_rejects_unsupported_content_type() -> None:
    downloader = VideoDownloader(
        Settings(), lambda _, __: FakeResponse([b"payload"], "text/html"), _public_resolver
    )
    with pytest.raises(DownloadError, match="content type"):
        with downloader.download(_url()):
            raise AssertionError("unsupported content type entered context")


def test_download_rejects_localhost_addresses() -> None:
    def localhost(host: str, port: int, **_: object) -> list[tuple[object, ...]]:
        del host
        return [(None, None, None, None, ("127.0.0.1", port))]

    downloader = VideoDownloader(Settings(), lambda _, __: FakeResponse([]), localhost)
    with pytest.raises(DownloadError, match="public IP"):
        with downloader.download(_url("http://localhost/video.mp4")):
            raise AssertionError("localhost entered context")
