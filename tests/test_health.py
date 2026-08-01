"""Health and application-import smoke tests."""

import asyncio

import httpx

from main import app


def test_fastapi_application_imports_and_exposes_docs() -> None:
    """The module-level FastAPI application is importable."""
    response = asyncio.run(_request("/openapi.json"))

    assert response.status_code == 200
    assert "/analyze" in response.json()["paths"]
    assert "/health" not in response.json()["paths"]


async def _request(path: str) -> httpx.Response:
    """Send an in-process ASGI request without a running web server."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)
