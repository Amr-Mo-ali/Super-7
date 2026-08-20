"""Controlled HTTP-admission smoke driver for an already-running Super-7 instance."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from time import perf_counter
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class RequestResult:
    index: int
    video_id: str
    player_id: str
    video_reference: str
    status_code: int | None
    admission_latency_ms: int
    analysis_id: str | None
    error_category: str | None
    error_message: str | None


def validate_options(args: argparse.Namespace) -> None:
    if args.request_count <= 0:
        raise ValueError("request count must be positive")
    if args.concurrency <= 0:
        raise ValueError("admission concurrency must be positive")
    if args.concurrency > args.request_count:
        raise ValueError("admission concurrency must not exceed request count")
    if args.timeout <= 0:
        raise ValueError("timeout must be positive")
    if not args.callback_url:
        raise ValueError("callback URL must be explicitly supplied")
    for reference in args.video_reference:
        _validate_reference(reference)


def build_payload(index: int, args: argparse.Namespace) -> dict[str, str]:
    return {
        "videoId": f"{args.video_prefix}-{index}",
        "playerId": f"{args.player_prefix}-{index}",
        "videoUrl": args.video_reference[index % len(args.video_reference)],
        "callbackUrl": args.callback_url,
    }


def submit(base_url: str, payload: dict[str, str], timeout: float) -> RequestResult:
    started = perf_counter()
    index = int(payload["videoId"].rsplit("-", 1)[1])
    try:
        request = Request(
            f"{base_url.rstrip('/')}/analyze",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - explicit operator URL
            status, body = response.status, response.read()
    except HTTPError as error:
        status, body = error.code, error.read()
    except (OSError, URLError, TimeoutError) as error:
        return RequestResult(
            index,
            payload["videoId"],
            payload["playerId"],
            payload["videoUrl"],
            None,
            _ms(started),
            None,
            type(error).__name__,
            "transport failure",
        )
    try:
        decoded = json.loads(body)
    except (TypeError, json.JSONDecodeError):
        decoded = {}
    analysis_id = decoded.get("analysisId") if isinstance(decoded, dict) else None
    if status == 202 and (not isinstance(analysis_id, str) or not analysis_id.strip()):
        return RequestResult(
            index,
            payload["videoId"],
            payload["playerId"],
            payload["videoUrl"],
            status,
            _ms(started),
            None,
            "MalformedResponse",
            "admitted response lacks analysis identity",
        )
    category = None if status == 202 else f"HTTP{status}"
    message = None if status == 202 else "admission rejected"
    return RequestResult(
        index,
        payload["videoId"],
        payload["playerId"],
        payload["videoUrl"],
        status,
        _ms(started),
        analysis_id,
        category,
        message,
    )


def summarize(results: list[RequestResult], duration_ms: int) -> dict[str, object]:
    latencies = [item.admission_latency_ms for item in results]
    statuses: dict[str, int] = {}
    for item in results:
        key = "transport" if item.status_code is None else str(item.status_code)
        statuses[key] = statuses.get(key, 0) + 1
    return {
        "attempted": len(results),
        "admitted": sum(x.status_code == 202 for x in results),
        "rejected": sum(x.status_code not in (None, 202) for x in results),
        "transport_failures": sum(x.status_code is None for x in results),
        "status_codes": statuses,
        "admission_latency_ms": {
            "min": min(latencies),
            "mean": statistics.mean(latencies),
            "max": max(latencies),
        },
        "driver_duration_ms": duration_ms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--callback-url", required=True)
    parser.add_argument("--video-reference", action="append", required=True)
    parser.add_argument("--request-count", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=10)
    parser.add_argument("--video-prefix", default="smoke-video")
    parser.add_argument("--player-prefix", default="smoke-player")
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args(argv)
    try:
        validate_options(args)
    except ValueError as error:
        parser.error(str(error))
    started = perf_counter()
    payloads = [build_payload(index, args) for index in range(args.request_count)]
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(
            executor.map(lambda payload: submit(args.base_url, payload, args.timeout), payloads)
        )
    report = {
        "metadata": {
            "schema_version": "process-pool-mvp-smoke-v1",
            "driver": "process_pool_mvp_smoke",
            "created_at_utc": datetime.now(UTC).isoformat(),
        },
        "options": {
            "base_url": _sanitize_url(args.base_url),
            "request_count": args.request_count,
            "concurrency": args.concurrency,
            "timeout": args.timeout,
            "video_references": args.video_reference,
        },
        "requests": [asdict(item) for item in results],
        "summary": summarize(results, _ms(started)),
    }
    rendered = json.dumps(report, indent=2, sort_keys=True)
    print(rendered)
    if args.json_output:
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return (
        0
        if all(
            item.status_code is not None and item.error_category != "MalformedResponse"
            for item in results
        )
        else 1
    )


def _validate_reference(value: str) -> None:
    path = Path(value)
    if (
        not value
        or "/" in value
        or "\\" in value
        or path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or ".." in path.parts
        or value != path.name
    ):
        raise ValueError("video references must be safe filenames")


def _ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _sanitize_url(value: str) -> str:
    parsed = urlsplit(value)
    hostname = parsed.hostname or ""
    netloc = hostname if parsed.port is None else f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


if __name__ == "__main__":
    sys.exit(main())
