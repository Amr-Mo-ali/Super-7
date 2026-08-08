"""Opt-in request-scoped performance measurement for local benchmarks."""

import os
import platform
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from time import perf_counter_ns, process_time_ns


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    rss_bytes: int | None
    cpu_time_ns: int
    thread_count: int
    open_handles: int | None
    gpu_enabled: bool
    cuda_device: str | None
    gpu_allocated_bytes: int | None
    gpu_peak_allocated_bytes: int | None


@dataclass(frozen=True, slots=True)
class PerformanceProfile:
    analysis_id: str
    stages_ns: dict[str, int]
    counters: dict[str, int]
    before: ResourceSnapshot
    after: ResourceSnapshot
    peak_rss_bytes: int | None
    video_duration_seconds: float | None = None
    frames_processed: int | None = None
    upload_bytes: int | None = None
    artifact_bytes: int = 0
    response_bytes: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "analysis_id": self.analysis_id,
            "stages_ms": {key: value / 1_000_000 for key, value in sorted(self.stages_ns.items())},
            "counters": dict(sorted(self.counters.items())),
            "before": asdict(self.before),
            "after": asdict(self.after),
            "peak_rss_bytes": self.peak_rss_bytes,
            "video_duration_seconds": self.video_duration_seconds,
            "frames_processed": self.frames_processed,
            "upload_bytes": self.upload_bytes,
            "artifact_bytes": self.artifact_bytes,
            "response_bytes": self.response_bytes,
            "realtime_factor": _ratio(
                self.stages_ns.get("total_pipeline", 0), self.video_duration_seconds
            ),
            "processing_ms_per_video_second": _milliseconds_per_second(
                self.stages_ns.get("total_pipeline", 0), self.video_duration_seconds
            ),
            "effective_processed_fps": _fps(
                self.frames_processed, self.stages_ns.get("total_pipeline", 0)
            ),
        }


class PerformanceCollector:
    """Collects one serial benchmark request without changing response contents."""

    def __init__(self, device: str) -> None:
        self._device = device
        self._analysis_id: str | None = None
        self._stages_ns: dict[str, int] = {}
        self._counters: dict[str, int] = {}
        self._before: ResourceSnapshot | None = None
        self._peak_rss_bytes: int | None = None
        self._video_duration_seconds: float | None = None
        self._frames_processed: int | None = None
        self._upload_bytes: int | None = None
        self._artifact_bytes = 0
        self._response_bytes: int | None = None

    def start(self, analysis_id: str) -> None:
        if self._analysis_id is not None:
            raise RuntimeError("PerformanceCollector records one request at a time.")
        self._analysis_id = analysis_id
        self._before = _snapshot(self._device)
        self.observe_resources()

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        started = perf_counter_ns()
        try:
            yield
        finally:
            self._stages_ns[name] = self._stages_ns.get(name, 0) + perf_counter_ns() - started
            self.observe_resources()

    def add_counter(self, name: str, value: int = 1) -> None:
        self._counters[name] = self._counters.get(name, 0) + value

    def set_video(self, duration_seconds: float, frames_processed: int, upload_bytes: int) -> None:
        self._video_duration_seconds = duration_seconds
        self._frames_processed = frames_processed
        self._upload_bytes = upload_bytes

    def set_artifact_bytes(self, value: int) -> None:
        self._artifact_bytes = value

    def set_response_bytes(self, value: int) -> None:
        self._response_bytes = value

    def finish(self) -> PerformanceProfile:
        if self._analysis_id is None or self._before is None:
            raise RuntimeError("PerformanceCollector was not started.")
        return PerformanceProfile(
            self._analysis_id,
            dict(self._stages_ns),
            dict(self._counters),
            self._before,
            _snapshot(self._device),
            self._peak_rss_bytes,
            self._video_duration_seconds,
            self._frames_processed,
            self._upload_bytes,
            self._artifact_bytes,
            self._response_bytes,
        )

    def observe_resources(self) -> None:
        rss = _rss_bytes()
        if rss is not None:
            self._peak_rss_bytes = max(self._peak_rss_bytes or rss, rss)


_collector: ContextVar[PerformanceCollector | None] = ContextVar(
    "performance_collector", default=None
)


@contextmanager
def use_collector(collector: PerformanceCollector | None) -> Iterator[None]:
    token = _collector.set(collector)
    try:
        yield
    finally:
        _collector.reset(token)


def current_collector() -> PerformanceCollector | None:
    return _collector.get()


def environment(device: str) -> dict[str, object]:
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "device": device,
        "pid": os.getpid(),
        "worker_count": 1,
        "max_active_analyses": 1,
    }


def _snapshot(device: str) -> ResourceSnapshot:
    gpu_enabled = False
    cuda_device: str | None = None
    allocated: int | None = None
    peak: int | None = None
    if device.startswith("cuda"):
        try:
            import torch

            if torch.cuda.is_available():
                gpu_enabled = True
                cuda_device = device
                allocated = int(torch.cuda.memory_allocated(device))
                peak = int(torch.cuda.max_memory_allocated(device))
        except Exception:
            pass
    return ResourceSnapshot(
        _rss_bytes(),
        process_time_ns(),
        threading.active_count(),
        None,
        gpu_enabled,
        cuda_device,
        allocated,
        peak,
    )


def _rss_bytes() -> int | None:
    try:
        import resource

        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # type: ignore[attr-defined]
        return int(value * 1024 if sys.platform != "darwin" else value)
    except ImportError:
        return None


def _ratio(total_ns: int, duration: float | None) -> float | None:
    return total_ns / 1_000_000_000 / duration if duration and duration > 0 else None


def _milliseconds_per_second(total_ns: int, duration: float | None) -> float | None:
    return total_ns / 1_000_000 / duration if duration and duration > 0 else None


def _fps(frames: int | None, total_ns: int) -> float | None:
    return frames / (total_ns / 1_000_000_000) if frames is not None and total_ns > 0 else None
