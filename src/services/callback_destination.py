"""Resolve callback destinations and connect only to checked numeric addresses."""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.client import HTTPConnection
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import Any
from urllib.parse import urlsplit

from pydantic import HttpUrl

type Resolver = Callable[..., list[tuple[Any, ...]]]


@dataclass(frozen=True, slots=True)
class CallbackDestination:
    hostname: str
    port: int
    address: IPv4Address | IPv6Address
    scheme: str
    host_header: str
    target: str


def resolve_callback_destination(url: HttpUrl, resolver: Resolver) -> CallbackDestination:
    """Reject the entire DNS answer set if any address is not public."""
    if url.scheme not in {"http", "https"}:
        raise ValueError("callback URL must use HTTP or HTTPS")
    parts = urlsplit(str(url))
    hostname = parts.hostname
    if hostname is None:
        raise ValueError("callback URL must include a host")
    port = parts.port or (443 if url.scheme == "https" else 80)
    addresses: list[IPv4Address | IPv6Address] = []
    for answer in resolver(hostname, port, type=socket.SOCK_STREAM):
        try:
            raw_address = str(answer[4][0])
            if "%" in raw_address:
                raise ValueError("Scoped addresses are not callback destinations.")
            address = ip_address(raw_address)
        except (IndexError, TypeError, ValueError) as error:
            raise ValueError("callback URL must resolve only to public IP addresses") from error
        if not address.is_global:
            raise ValueError("callback URL must resolve only to public IP addresses")
        addresses.append(address)
    if not addresses:
        raise ValueError("callback URL must resolve only to public IP addresses")
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if url.scheme == "https" else 80
    if port != default_port:
        host_header = f"{host_header}:{port}"
    target = parts.path or "/"
    if parts.query:
        target = f"{target}?{parts.query}"
    return CallbackDestination(hostname, port, addresses[0], url.scheme, host_header, target)


class _PinnedConnection(HTTPConnection):
    """Use HTTPConnection's public connect hook without resolving the hostname again."""

    def __init__(self, destination: CallbackDestination, timeout: float) -> None:
        super().__init__(destination.hostname, destination.port, timeout=timeout)
        self._destination = destination

    def connect(self) -> None:
        address = self._destination.address
        family = socket.AF_INET6 if isinstance(address, IPv6Address) else socket.AF_INET
        raw = socket.socket(family, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.connect((str(address), self.port))
            self.sock = (
                ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
                if self._destination.scheme == "https"
                else raw
            )
        except BaseException:
            raw.close()
            raise


def post_pinned(
    destination: CallbackDestination,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> int:
    """Send one HTTP/1.1 request with the original Host and TLS identity."""
    connection = _PinnedConnection(destination, timeout)
    try:
        connection.request(
            "POST",
            destination.target,
            body=body,
            headers={**headers, "Host": destination.host_header},
        )
        return connection.getresponse().status
    finally:
        connection.close()
