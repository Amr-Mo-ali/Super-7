"""Secure callback transport shared by combined and dispatcher-only roles."""

from collections.abc import Mapping

import httpx
from pydantic import HttpUrl

from services.analysis_callback_delivery import CallbackTransport
from services.callback_service import CallbackService


def create_callback_transport(validator: CallbackService) -> CallbackTransport:
    async def post(url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        try:
            destination = validator.resolve_callback_destination(HttpUrl(url))
        except ValueError as error:
            raise OSError("Callback destination unavailable.") from error
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
