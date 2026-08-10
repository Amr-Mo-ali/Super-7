# Phase 11 - Public URL ingestion

## Objective

`POST /analyze` now accepts a public video URL instead of a multipart video upload. The downloaded
file remains request-scoped and is passed to the existing local-file analysis pipeline unchanged.

## Request contract

```json
{
  "video_url": "https://cdn.example.com/video_008.mp4",
  "metadata": {
    "video_id": "video_008",
    "player_ai_id": "player_123"
  }
}
```

`video_url` is a Pydantic `HttpUrl`. `metadata` is optional `dict[str, Any]`; it is deep-copied at
the request boundary, never interpreted, and preserves JSON key order and values.

## Response contract

Every Public Rating V2 success or non-completed response now includes `request_id` and `metadata`.
`request_id` equals the existing analysis identifier. The metadata payload is emitted unchanged.

## Security model

The downloader accepts HTTP and HTTPS only, resolves the hostname before opening a connection, and
rejects any hostname with a non-global address (loopback, private, link-local, multicast,
unspecified, or reserved). Redirects are refused so a validated URL cannot redirect to an internal
destination. It requires a `video/*` content type, a supported video filename extension, a
successful 2xx status, and enforces `MAX_UPLOAD_BYTES` from headers and while streaming.

Each upstream request uses `DOWNLOAD_TIMEOUT_SECONDS` (30 seconds by default). Download chunks
check the existing request cancellation token before writing each chunk.

## Cleanup behavior

The downloader creates a named temporary file only after URL and response validation. Its context
manager removes that file in `finally`, whether the download, validation, cancellation, or analysis
path succeeds or fails. The pre-existing lifecycle continues to own artifacts and analysis cleanup.

## Tests and verification

Focused tests cover valid URL handling, invalid URL rejection, timeout translation, size limits,
content-type rejection, localhost rejection, metadata propagation, and temporary-file cleanup.

Verification command:

```powershell
uv run pytest tests/test_analyze.py tests/test_video_io_safety.py
uv run ruff check src tests
uv run mypy src tests
```
