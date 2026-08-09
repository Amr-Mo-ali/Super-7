"""Run serial, opt-in request-path benchmarks and write JSON results."""

import argparse
import asyncio
import json
from pathlib import Path
from statistics import median
from time import perf_counter_ns

import httpx

from core.config import Settings
from diagnostics.performance import PerformanceCollector, environment, use_collector
from main import app


async def _run(video: Path, repeat: int, warmup: bool) -> dict[str, object]:
    transport = httpx.ASGITransport(app=app)
    runs: list[dict[str, object]] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://benchmark") as client:
        for index in range(repeat + int(warmup)):
            collector = PerformanceCollector(Settings.from_environment().model_device)
            collector.start(f"benchmark-{video.stem}-{index}")
            started = perf_counter_ns()
            with (
                video.open("rb") as handle,
                use_collector(collector),
                collector.stage("total_pipeline"),
            ):
                response = await client.post(
                    "/analyze", files={"video": (video.name, handle, "application/octet-stream")}
                )
            collector.set_response_bytes(len(response.content))
            profile = collector.finish().as_dict()
            profile["total_request_ms"] = (perf_counter_ns() - started) / 1_000_000
            profile["http_status"] = response.status_code
            if index >= int(warmup):
                runs.append(profile)
    totals = [float(item["total_request_ms"]) for item in runs]
    return {
        "name": video.name,
        "input_bytes": video.stat().st_size,
        "runs": runs,
        "summary": {
            "total_request_ms": {"min": min(totals), "median": median(totals), "max": max(totals)}
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", action="append", required=True, type=Path)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--warmup", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.repeat <= 0:
        raise SystemExit("--repeat must be positive")
    missing = [path for path in args.video if not path.is_file()]
    if missing:
        raise SystemExit(f"video paths do not exist: {', '.join(str(path) for path in missing)}")
    payload = {
        "environment": environment(Settings.from_environment().model_device),
        "model_startup": "not separated by the current composition path",
        "videos": asyncio.run(_all(args.video, args.repeat, args.warmup)),
    }
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


async def _all(videos: list[Path], repeat: int, warmup: bool) -> list[dict[str, object]]:
    return [await _run(video, repeat, warmup) for video in videos]


if __name__ == "__main__":
    main()
