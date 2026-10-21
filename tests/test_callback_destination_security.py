"""Network-free controls for both production callback connection boundaries."""

import asyncio
import io
import logging
import socket
import ssl
from collections.abc import Mapping
from unittest.mock import patch

import anyio
import anyio.streams.tls
import httpx
import pytest
from pydantic import HttpUrl

from durable_local_main import _transport
from services.callback_service import CallbackPayload, CallbackService, DetailedRatings


def _answers(*addresses: str, port: int) -> list[tuple[object, ...]]:
    return [
        (
            socket.AF_INET6 if ":" in address else socket.AF_INET,
            socket.SOCK_STREAM,
            6,
            "",
            (address, port),
        )
        for address in addresses
    ]


def _service(resolver: object) -> CallbackService:
    return CallbackService(1, logging.getLogger("callback.boundary"), resolver=resolver)  # type: ignore[arg-type]


async def _post_durable(
    validator: CallbackService,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> int:
    return await _transport(validator)(url, body, headers, timeout)


class FakeSocket:
    attempted: list[tuple[str, int]] = []
    sent: list[bytes] = []

    def __init__(self, *_args: object) -> None:
        pass

    def settimeout(self, _value: float) -> None:
        pass

    def connect(self, address: tuple[str, int]) -> None:
        self.attempted.append(address)

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def makefile(self, *_args: object) -> io.BytesIO:
        return io.BytesIO(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")

    def close(self) -> None:
        pass


@pytest.mark.parametrize(
    "status,expected_attempts", [(200, 1), (204, 1), (302, 4), (400, 4), (500, 4)]
)
def test_legacy_send_status_retry_and_redirect_contract(
    status: int,
    expected_attempts: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class ReplySocket(FakeSocket):
        def makefile(self, *_args: object) -> io.BytesIO:
            return io.BytesIO(
                f"HTTP/1.1 {status} Result\r\nContent-Length: 0\r\nLocation: http://127.0.0.1/\r\n\r\n".encode()
            )

    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    ReplySocket.attempted = []
    ReplySocket.sent = []
    service = CallbackService(
        1,
        logging.getLogger("callback.boundary.send"),
        resolver=lambda _host, port, **_kwargs: _answers("8.8.8.8", port=port),
        sleep=sleep,
    )
    payload = CallbackPayload(
        request_id="request-1",
        video_id="video-1",
        player_id="player-1",
        status="completed",
        summary={},
        ratings={},
        detailed=DetailedRatings(),
        events={},
    )
    caplog.set_level(logging.INFO, logger="callback.boundary.send")

    async def run_with_controlled_socket() -> bool:
        with monkeypatch.context() as network:
            network.setattr(socket, "socket", ReplySocket)
            network.setattr(
                socket,
                "getaddrinfo",
                lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("DNS lookup")),
            )
            return await service.send_result(HttpUrl("http://callback.example/hook"), payload)

    delivered = asyncio.run(run_with_controlled_socket())
    assert delivered is (status in {200, 204})
    assert ReplySocket.attempted == [("8.8.8.8", 80)] * expected_attempts
    assert delays == ([] if status in {200, 204} else [1.0, 2.0, 4.0])
    assert (
        sum("analysis_callback_attempt_finished" in r.message for r in caplog.records)
        == expected_attempts
    )
    assert sum("callback_outcome=delivered" in r.message for r in caplog.records) == (
        2 if status in {200, 204} else 0
    )
    assert sum("callback_outcome=exhausted" in r.message for r in caplog.records) == (
        0 if status in {200, 204} else 1
    )


@pytest.mark.parametrize("status,certificate_failure", [(204, False), (302, False), (204, True)])
def test_durable_real_httpcore_connection_boundary_without_network(
    status: int,
    certificate_failure: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connections: list[tuple[str, int]] = []
    tls: list[tuple[str | None, bool, ssl.VerifyMode]] = []
    sent: list[bytes] = []

    class ControlledStream:
        def __init__(self) -> None:
            self._response = (
                f"HTTP/1.1 {status} Result\r\nContent-Length: 0\r\n"
                "Location: http://127.0.0.1/\r\n\r\n"
            ).encode()

        async def send(self, item: bytes) -> None:
            sent.append(item)

        async def receive(self, max_bytes: int = 65536) -> bytes:
            response, self._response = self._response[:max_bytes], self._response[max_bytes:]
            if not response:
                raise anyio.EndOfStream
            return response

        async def aclose(self) -> None:
            pass

        def extra(self, _attribute: object, default: object = None) -> object:
            return default

    async def connect_tcp(
        *, remote_host: str, remote_port: int, local_host: str | None = None
    ) -> ControlledStream:
        del local_host
        connections.append((remote_host, remote_port))
        assert remote_host == "8.8.8.8"
        return ControlledStream()

    async def wrap_tls(
        stream: ControlledStream,
        *,
        ssl_context: ssl.SSLContext,
        hostname: str | None,
        standard_compatible: bool,
        server_side: bool,
    ) -> ControlledStream:
        assert standard_compatible is False and server_side is False
        tls.append((hostname, ssl_context.check_hostname, ssl_context.verify_mode))
        if certificate_failure:
            raise ssl.SSLCertVerificationError("controlled certificate failure")
        return stream

    monkeypatch.setattr(anyio, "connect_tcp", connect_tcp)
    monkeypatch.setattr(anyio.streams.tls.TLSStream, "wrap", staticmethod(wrap_tls))
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("DNS lookup")),
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    validator = _service(lambda _host, port, **_kwargs: _answers("8.8.8.8", port=port))
    if certificate_failure:
        with pytest.raises(OSError, match="Callback transport unavailable") as error:
            asyncio.run(_post_durable(validator, "https://callback.example/hook", b"{}", {}, 1))
        assert isinstance(error.value.__cause__, httpx.ConnectError)
        assert "controlled certificate failure" in str(error.value.__cause__)
    else:
        assert (
            asyncio.run(_post_durable(validator, "https://callback.example/hook", b"{}", {}, 1))
            == status
        )
        assert b"Host: callback.example\r\n" in b"".join(sent)
    assert connections == [("8.8.8.8", 443)]
    assert tls == [("callback.example", True, ssl.CERT_REQUIRED)]


def test_legacy_rebind_rejects_private_delivery_answer_before_dial() -> None:
    calls = 0

    def resolver(_host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        nonlocal calls
        calls += 1
        return _answers("8.8.8.8" if calls == 1 else "127.0.0.1", port=port)

    service = _service(resolver)
    url = "http://rebind.example/callback"
    service.validate_callback_url(HttpUrl(url))
    FakeSocket.attempted = []
    with patch("socket.socket", FakeSocket):
        with pytest.raises(OSError, match="destination unavailable"):
            service._post(url, b"{}", 1)
    assert calls == 2
    assert FakeSocket.attempted == []


def test_durable_rebind_rejects_before_client_construction() -> None:
    calls = 0

    def resolver(_host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
        nonlocal calls
        calls += 1
        return _answers("8.8.8.8" if calls == 1 else "127.0.0.1", port=port)

    validator = _service(resolver)
    validator.validate_callback_url(HttpUrl("http://rebind.example/callback"))

    class ForbiddenClient:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("unchecked HTTPX client was constructed")

    with patch("durable_local_main.httpx.AsyncClient", ForbiddenClient):
        with pytest.raises(OSError, match="destination unavailable"):
            asyncio.run(_post_durable(validator, "http://rebind.example/callback", b"{}", {}, 1))
    assert calls == 2


@pytest.mark.parametrize("addresses", [("8.8.8.8", "127.0.0.1"), ("2606:4700:4700::1111", "::1")])
def test_mixed_answers_rejected_before_either_transport(addresses: tuple[str, str]) -> None:
    service = _service(lambda _host, port, **_kwargs: _answers(*addresses, port=port))
    with patch("socket.socket", side_effect=AssertionError("socket opened")):
        with pytest.raises(OSError, match="destination unavailable"):
            service._post("https://mixed.example/hook", b"{}", 1)
    with patch("durable_local_main.httpx.AsyncClient", side_effect=AssertionError("client opened")):
        with pytest.raises(OSError, match="destination unavailable"):
            asyncio.run(_post_durable(service, "https://mixed.example/hook", b"{}", {}, 1))


@pytest.mark.parametrize("status", [204, 302])
def test_legacy_https_pins_ipv6_tls_host_bytes_and_ignores_proxy(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ReplySocket(FakeSocket):
        def makefile(self, *_args: object) -> io.BytesIO:
            return io.BytesIO(
                f"HTTP/1.1 {status} Result\r\nContent-Length: 0\r\nLocation: http://127.0.0.1/\r\n\r\n".encode()
            )

    seen_names: list[str] = []

    class FakeContext:
        check_hostname = True
        verify_mode = ssl.CERT_REQUIRED

        def wrap_socket(self, sock: ReplySocket, *, server_hostname: str) -> ReplySocket:
            seen_names.append(server_hostname)
            return sock

    ReplySocket.attempted = []
    ReplySocket.sent = []
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    service = _service(lambda _host, port, **_kwargs: _answers("2606:4700:4700::1111", port=port))
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("hostname lookup")),
        patch("socket.socket", ReplySocket),
        patch(
            "services.callback_destination.ssl.create_default_context", return_value=FakeContext()
        ),
    ):
        result = service._post("https://callback.example:8443/hook?q=1", b'{"ok":true}', 2)
    assert result == status
    assert ReplySocket.attempted == [("2606:4700:4700::1111", 8443)]
    assert seen_names == ["callback.example"]
    request = b"".join(ReplySocket.sent)
    assert request.startswith(b"POST /hook?q=1 HTTP/1.1\r\n")
    assert b"Host: callback.example:8443\r\n" in request
    assert b"Content-Type: application/json\r\n" in request
    assert b"Accept: application/json\r\n" in request
    assert request.endswith(b'{"ok":true}')


@pytest.mark.parametrize("status", [204, 302])
@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_durable_pins_ip_preserves_tls_host_headers_and_ignores_proxy(
    status: int, address: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[httpx.Request] = []
    options: list[dict[str, object]] = []
    original_client = httpx.AsyncClient

    def make_client(**kwargs: object) -> httpx.AsyncClient:
        options.append(kwargs)
        follow_redirects = kwargs["follow_redirects"]
        trust_env = kwargs["trust_env"]
        timeout = kwargs["timeout"]
        assert isinstance(follow_redirects, bool)
        assert isinstance(trust_env, bool)
        assert isinstance(timeout, (int, float))

        async def receive(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(status, headers={"Location": "http://127.0.0.1/"})

        return original_client(
            follow_redirects=follow_redirects,
            trust_env=trust_env,
            timeout=timeout,
            transport=httpx.MockTransport(receive),
        )

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8888")
    service = _service(lambda _host, port, **_kwargs: _answers(address, port=port))
    with patch("durable_local_main.httpx.AsyncClient", make_client):
        result = asyncio.run(
            _post_durable(
                service,
                "https://callback.example:8443/hook?q=1",
                b'{"ok":true}',
                {"X-Event-Id": "event-1"},
                2,
            )
        )
    assert result == status
    assert options == [{"follow_redirects": False, "trust_env": False, "timeout": 2}]
    assert len(captured) == 1
    request = captured[0]
    assert request.url.host == address
    assert request.url.port == 8443
    assert request.url.path == "/hook"
    assert request.url.query == b"q=1"
    assert request.content == b'{"ok":true}'
    assert request.headers["X-Event-Id"] == "event-1"
    assert request.headers["Host"] == "callback.example:8443"
    assert request.extensions["sni_hostname"] == "callback.example"
