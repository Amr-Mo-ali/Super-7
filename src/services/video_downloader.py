"""Secure, bounded retrieval of public video URLs."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from email.message import Message
from ipaddress import ip_address
from pathlib import Path
from socket import SOCK_STREAM, getaddrinfo
from tempfile import NamedTemporaryFile
from typing import Any, Final, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import HttpUrl

from concurrency.cancellation import CancellationManager
from concurrency.exceptions import AnalysisCancelled
from core.config import Settings
from core.exceptions import (
    DownloadError,
    DownloadTimeoutError,
    InvalidVideoError,
    UploadTooLargeError,
)

_CHUNK_SIZE: Final = 1024 * 1024
_SUPPORTED_SUFFIXES: Final = frozenset({".avi", ".mkv", ".mov", ".mp4"})


class _Response(Protocol):
    headers: Message

    def __enter__(self) -> "_Response": ...

    def __exit__(self, *args: object) -> None: ...

    def read(self, amount: int = -1) -> bytes: ...

    def getcode(self) -> int | None: ...


class _RejectRedirects(HTTPRedirectHandler):
    """Refuse redirects so a validated public URL cannot pivot to an internal target."""

    def redirect_request(self, *args: object, **kwargs: object) -> Request | None:
        del args, kwargs
        return None


class VideoDownloader:
    """Download one public video to a request-owned temporary path."""

    def __init__(
        self,
        settings: Settings,
        opener: Callable[[Request, float], _Response] | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = getaddrinfo,
    ) -> None:
        self._settings = settings
        self._opener = opener or self._open
        self._resolver = resolver

    @contextmanager
    def download(
        self, video_url: HttpUrl, cancellation: CancellationManager | None = None
    ) -> Iterator[Path]:
        """Stream a validated public URL to disk and always remove the temporary file."""
        url = str(video_url)
        self._validate_url(video_url)
        suffix = Path(video_url.path or "").suffix.lower()
        if suffix not in _SUPPORTED_SUFFIXES:
            raise InvalidVideoError("Unsupported video format. Use MP4, MOV, AVI, or MKV.")
        temporary_path: Path | None = None
        try:
            request = Request(url, headers={"Accept": "video/*"})
            with self._response(request) as response:
                status = response.getcode()
                if status is None or not 200 <= status < 300:
                    raise DownloadError("Video URL returned a non-successful response.")
                content_type = response.headers.get_content_type()
                if not content_type.startswith("video/"):
                    raise DownloadError("Video URL returned an unsupported content type.")
                content_length = response.headers.get("Content-Length")
                if content_length is not None and int(content_length) > self._settings.max_upload_bytes:
                    raise UploadTooLargeError("Downloaded video exceeds the configured size limit.")
                with NamedTemporaryFile(delete=False, suffix=suffix) as temporary_file:
                    temporary_path = Path(temporary_file.name)
                    size = 0
                    while chunk := response.read(_CHUNK_SIZE):
                        if cancellation is not None and cancellation.is_cancelled():
                            raise AnalysisCancelled("Analysis cancelled during video download.")
                        size += len(chunk)
                        if size > self._settings.max_upload_bytes:
                            raise UploadTooLargeError(
                                "Downloaded video exceeds the configured size limit."
                            )
                        temporary_file.write(chunk)
            yield temporary_path
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _validate_url(self, video_url: HttpUrl) -> None:
        if video_url.scheme not in {"http", "https"}:
            raise DownloadError("Only HTTP and HTTPS video URLs are supported.")
        host = video_url.host
        if host is None:
            raise DownloadError("Video URL must include a host.")
        port = video_url.port or (443 if video_url.scheme == "https" else 80)
        try:
            resolved = self._resolver(host, port, type=SOCK_STREAM)
        except OSError as error:
            raise DownloadError("Video URL host could not be resolved.") from error
        addresses = {str(item[4][0]) for item in resolved}
        if not addresses or any(not ip_address(address).is_global for address in addresses):
            raise DownloadError("Video URL must resolve only to public IP addresses.")

    def _response(self, request: Request) -> _Response:
        try:
            return self._opener(request, self._settings.download_timeout_seconds)
        except TimeoutError as error:
            raise DownloadTimeoutError("Video download timed out.") from error
        except HTTPError as error:
            raise DownloadError("Video URL returned a non-successful response.") from error
        except URLError as error:
            if isinstance(error.reason, TimeoutError):
                raise DownloadTimeoutError("Video download timed out.") from error
            raise DownloadError("Video URL could not be downloaded.") from error
        except OSError as error:
            raise DownloadError("Video URL could not be downloaded.") from error

    @staticmethod
    def _open(request: Request, timeout: float) -> _Response:
        return cast(_Response, build_opener(_RejectRedirects()).open(request, timeout=timeout))
