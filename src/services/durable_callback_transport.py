"""Secure callback transport shared by combined and dispatcher-only roles."""

from collections.abc import Mapping

import httpx
from pydantic import HttpUrl

from services.analysis_callback_delivery import CallbackTransport
from services.callback_service import CallbackService
from services.durable_callback_https_policy import require_https_callback


def create_callback_transport(
    validator: CallbackService, *, https_only_callbacks: bool = False
) -> CallbackTransport:
    async def post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        try:
            parsed_url = HttpUrl(url)
            if https_only_callbacks:
                require_https_callback(parsed_url)
            destination = validator.resolve_callback_destination(parsed_url)
        except ValueError:
            raise OSError("Callback destination unavailable.") from None
        pinned_url = httpx.URL(url).copy_with(host=str(destination.address))
        try:
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=False, timeout=timeout
            ) as client:
                response = await client.post(
                    pinned_url,
                    content=body,
                    headers={**headers, "Host": destination.host_header},
                    extensions={"sni_hostname": destination.hostname},
                )
            return response.status_code
        except httpx.TransportError as error:
            raise OSError("Callback transport unavailable.") from error

    return post
