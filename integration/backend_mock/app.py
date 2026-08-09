"""Minimal callback receiver used by the Phase 11.6 Compose verification harness."""

from typing import Any

from fastapi import FastAPI, status

app = FastAPI(title="Super-7 backend integration mock")
_callbacks: list[dict[str, Any]] = []
_database_updates: dict[str, dict[str, Any]] = {}


@app.get("/health/live")
async def live() -> dict[str, str]:
    """Provide a Compose health check without analysis-side dependencies."""
    return {"status": "ok"}


@app.post("/webhook", status_code=status.HTTP_204_NO_CONTENT)
async def webhook(payload: dict[str, Any]) -> None:
    """Record the callback and simulate the backend's video-analysis database update."""
    _callbacks.append(payload)
    video_id = payload.get("video_id")
    if isinstance(video_id, str):
        _database_updates[video_id] = payload


@app.get("/callbacks")
async def callbacks() -> dict[str, Any]:
    """Return received callbacks and simulated persisted backend state."""
    return {"callbacks": _callbacks, "database_updates": _database_updates}


@app.delete("/callbacks", status_code=status.HTTP_204_NO_CONTENT)
async def clear_callbacks() -> None:
    """Reset the mock's in-memory database between manual verification runs."""
    _callbacks.clear()
    _database_updates.clear()
