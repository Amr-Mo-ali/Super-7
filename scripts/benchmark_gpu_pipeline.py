"""Opt-in, bounded compute-only benchmark through the real spawned child entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import multiprocessing
import os
import platform
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import Future, ProcessPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from importlib.metadata import distributions
from multiprocessing.process import BaseProcess
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any
from unittest.mock import patch

import cv2
import psutil  # type: ignore[import-untyped]

from core.config import Settings
from core.logging import get_logger
from services import process_entrypoint
from services.process_contracts import (
    ChildAnalysisFailure,
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    validate_child_result,
)
from services.process_entrypoint import initialize_analysis_child, run_child_analysis


class BenchmarkDeviceError(RuntimeError):
    """Requested GPU execution cannot be verified."""


@dataclass(frozen=True)
class ChildMeasurement:
    pid: int
    wall_ms: float
    result: ChildAnalysisSuccess | ChildAnalysisFailure | Any
    torch_version: str
    torchvision_version: str
    torch_cuda_version: str | None
    cuda_device_name: str | None
    cuda_peak_allocated_bytes: int | None
    cuda_peak_reserved_bytes: int | None
    observed_execution_devices: dict[str, dict[str, Any]]
    device_failure_reason: str | None


@contextmanager
def _capture_inference_devices() -> Iterator[dict[int, set[str]]]:
    """Observe the preprocessed tensor handed to each Ultralytics inference call."""
    from ultralytics.engine.predictor import BasePredictor

    original = BasePredictor.inference
    observed: dict[int, set[str]] = {}

    def traced(predictor: Any, image: Any, *args: Any, **kwargs: Any) -> Any:
        output = original(predictor, image, *args, **kwargs)
        observed.setdefault(id(predictor), set()).add(str(image.device))
        return output

    with patch.object(BasePredictor, "inference", traced):
        yield observed


def _observed_execution_devices(observed: dict[int, set[str]]) -> dict[str, dict[str, Any]]:
    runtime = process_entrypoint._runtime
    if runtime is None:
        raise BenchmarkDeviceError("analysis child runtime is unavailable for device verification")
    tracker = runtime.components.tracker
    devices: dict[str, dict[str, Any]] = {}
    for name, detector in (("player", tracker._detector), ("ball", tracker._ball_detector)):
        model = getattr(detector, "_model", None)
        predictor = getattr(model, "predictor", None)
        backend = getattr(predictor, "model", None)
        devices[name] = {
            "inference_input_devices": sorted(observed.get(id(predictor), set())),
            "model_backend_device": str(backend.device) if backend is not None else None,
        }
    return devices


def _device_failure_reason(requested: str, observed: dict[str, dict[str, Any]]) -> str | None:
    for name in ("player", "ball"):
        evidence = observed[name]
        inputs = evidence["inference_input_devices"]
        backend = evidence["model_backend_device"]
        if inputs != [requested] or backend != requested:
            return (
                f"{name} execution device unverified: requested {requested}, "
                f"inference inputs {inputs}, model backend {backend}"
            )
    return None


def _initialize(settings: Settings, log_root: str) -> None:
    initialize_analysis_child(settings)
    handler = logging.FileHandler(Path(log_root) / f"child-{os.getpid()}.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    get_logger("football_analysis").addHandler(handler)
    get_logger("football_analysis.child").info("benchmark_child_ready child_pid=%s", os.getpid())


def _analyze(request: ChildAnalysisRequest, device: str) -> ChildMeasurement:
    import torch
    import torchvision  # type: ignore[import-untyped]

    cuda = device.startswith("cuda")
    if cuda and not torch.cuda.is_available():
        raise BenchmarkDeviceError("CUDA requested but torch.cuda.is_available() is false")
    if cuda:
        torch.cuda.synchronize(device)
        torch.cuda.reset_peak_memory_stats(device)
    started = perf_counter()
    with _capture_inference_devices() as inference_devices:
        result = run_child_analysis(request)
    if cuda:
        torch.cuda.synchronize(device)
    observed = _observed_execution_devices(inference_devices)
    return ChildMeasurement(
        os.getpid(),
        round((perf_counter() - started) * 1000, 3),
        result,
        torch.__version__,
        torchvision.__version__,
        torch.version.cuda,
        torch.cuda.get_device_name(device) if cuda else None,
        torch.cuda.max_memory_allocated(device) if cuda else None,
        torch.cuda.max_memory_reserved(device) if cuda else None,
        observed,
        _device_failure_reason(device, observed),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _video_metadata(path: Path) -> dict[str, Any]:
    capture = cv2.VideoCapture(str(path))
    try:
        if not capture.isOpened():
            raise ValueError(f"cannot open video: {path.name}")
        fps = capture.get(cv2.CAP_PROP_FPS)
        frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        return {
            "name": path.name,
            "sha256": _sha256(path),
            "bytes": path.stat().st_size,
            "fps": fps,
            "frame_count": frames,
            "duration_seconds": frames / fps if fps > 0 else None,
            "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        }
    finally:
        capture.release()


def _gpu_process_memory() -> dict[int, int] | None:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_gpu_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    readings: dict[int, int] = {}
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"\s*(\d+)\s*,\s*(\d+)\s*", line)
        if match:
            readings[int(match.group(1))] = int(match.group(2)) * 1024 * 1024
    return readings


def _device_inventory() -> dict[str, Any]:
    inventory: dict[str, Any] = {
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "logical_cpus": psutil.cpu_count(logical=True),
        "physical_cpus": psutil.cpu_count(logical=False),
        "system_ram_bytes": psutil.virtual_memory().total,
        "gpu": None,
    }
    try:
        gpu = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,uuid,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        inventory["gpu"] = gpu.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pass
    return inventory


def _sample_resources(
    peaks: dict[int, dict[str, int | None]], workers: dict[int, BaseProcess], gpu: bool
) -> None:
    gpu_memory = _gpu_process_memory() if gpu else None
    for pid, worker in workers.items():
        try:
            if not worker.is_alive():
                continue
            child = psutil.Process(pid)
            if not child.is_running():
                continue
            sample = peaks.setdefault(pid, {"rss_bytes": 0, "nvidia_smi_vram_bytes": None})
            sample["rss_bytes"] = max(int(sample["rss_bytes"] or 0), child.memory_info().rss)
            if gpu_memory is not None and pid in gpu_memory:
                sample["nvidia_smi_vram_bytes"] = max(
                    int(sample["nvidia_smi_vram_bytes"] or 0), gpu_memory[pid]
                )
        except (psutil.Error, OSError):
            continue


def _track_workers(executor: ProcessPoolExecutor, owned: dict[int, BaseProcess]) -> None:
    # Python 3.12 exposes no public worker handles. Keep only this executor's processes.
    processes = executor._processes
    if processes is None:
        raise RuntimeError("benchmark executor worker registry is unavailable")
    for pid, process in processes.items():
        if process.pid != pid:
            raise RuntimeError("benchmark executor worker identity is inconsistent")
        owned[pid] = process


def _cleanup_workers(
    workers: dict[int, BaseProcess], *, stop_requested: bool, grace_seconds: float = 3.0
) -> dict[str, Any]:
    """Bound each exit phase across all owned workers; never infer exit from a signal."""
    records: dict[int, dict[str, Any]] = {
        pid: {
            "pid": pid,
            "terminate_requested": False,
            "kill_requested": False,
            "errors": [],
            "exit_verified": False,
            "exitcode": None,
        }
        for pid in workers
    }

    def alive(pid: int, process: BaseProcess) -> bool | None:
        try:
            return process.is_alive()
        except Exception as error:
            records[pid]["errors"].append(f"liveness inspection: {type(error).__name__}")
            return None

    def request(signal: str, targets: list[int]) -> None:
        for pid in targets:
            try:
                getattr(workers[pid], signal)()
                records[pid][f"{signal}_requested"] = True
            except Exception as error:
                records[pid]["errors"].append(f"{signal}: {type(error).__name__}")

    def wait_for_exit() -> list[int]:
        deadline = monotonic() + grace_seconds
        unresolved: list[int] = []
        for pid, process in workers.items():
            if alive(pid, process) is not False:
                try:
                    process.join(timeout=max(0.0, deadline - monotonic()))
                except Exception as error:
                    records[pid]["errors"].append(f"join: {type(error).__name__}")
            if alive(pid, process) is not False:
                unresolved.append(pid)
        return unresolved

    if stop_requested:
        request("terminate", list(workers))
    remaining = wait_for_exit()
    if not stop_requested and remaining:
        request("terminate", remaining)
        remaining = wait_for_exit()
    if remaining:
        request("kill", remaining)
        wait_for_exit()
    unresolved = []
    for pid, process in workers.items():
        state = alive(pid, process)
        records[pid]["exit_verified"] = state is False and not any(
            error.startswith("liveness inspection:") for error in records[pid]["errors"]
        )
        if records[pid]["exit_verified"]:
            try:
                records[pid]["exitcode"] = process.exitcode
            except Exception as error:
                records[pid]["errors"].append(f"exitcode: {type(error).__name__}")
        else:
            unresolved.append(pid)
    return {
        "workers": list(records.values()),
        "forced_termination_pids": [
            pid for pid, record in records.items() if record["kill_requested"]
        ],
        "unresolved_processes": unresolved,
        "cleanup_verified": not unresolved,
    }


def _lifecycle_issues(report: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    cleanup = report.get("worker_cleanup")
    if not isinstance(cleanup, dict):
        return ["worker_cleanup_missing"]
    if cleanup.get("cleanup_verified") is not True or cleanup.get("unresolved_processes") != []:
        issues.append("worker_exit_unverified")
    if cleanup.get("forced_termination_pids"):
        issues.append("worker_kill_required")
    workers = cleanup.get("workers")
    if not isinstance(workers, list) or not workers:
        issues.append("worker_records_missing")
    else:
        for worker in workers:
            if not isinstance(worker, dict):
                issues.append("worker_record_invalid")
                continue
            pid = worker.get("pid")
            if worker.get("exit_verified") is not True:
                issues.append(f"worker_{pid}:exit_unverified")
            if worker.get("terminate_requested") or worker.get("kill_requested"):
                issues.append(f"worker_{pid}:shutdown_escalated")
            if type(worker.get("exitcode")) is not int or worker["exitcode"] != 0:
                issues.append(f"worker_{pid}:exitcode_nonzero_or_unavailable")
            if worker.get("errors"):
                issues.append(f"worker_{pid}:cleanup_errors")
    for field in ("worker_tracking_error", "executor_shutdown_error", "cancellation_errors"):
        if report.get(field):
            issues.append(field)
    return issues


def _log_evidence(root: Path, pid: int, analysis_id: str) -> dict[str, Any]:
    path = root / f"child-{pid}.log"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    stage_lines = [
        line
        for line in lines
        if "analysis_stage_timing " in line and f"analysis_id={analysis_id} " in line
    ]
    return {
        "child_log": path.name if path.exists() else None,
        "stage_timing_lines": stage_lines,
        "child_ready_lines": [line for line in lines if "benchmark_child_ready " in line],
        "player_model_load_lines": [line for line in lines if "model_loaded " in line],
        "ball_model_load_lines": [line for line in lines if "ball_model_loaded " in line],
        "first_player_inference_line": next(
            (line for line in lines if "player_inference frame=0 " in line), None
        ),
        "first_ball_inference_line": next(
            (line for line in lines if "ball_inference frame=0 " in line), None
        ),
    }


def _job_record(
    measurement: ChildMeasurement,
    request: ChildAnalysisRequest,
    requested_device: str,
    submitted: float,
    peaks: dict[int, dict[str, int | None]],
    log_root: Path,
) -> dict[str, Any]:
    result = measurement.result
    validated = validate_child_result(request.analysis_id, request.schema_version, result)
    record: dict[str, Any] = {
        "analysis_id": request.analysis_id,
        "video_name": request.video_reference,
        "pid": measurement.pid,
        "submit_to_result_ms": round((perf_counter() - submitted) * 1000, 3),
        "child_wall_ms": measurement.wall_ms,
        "child_duration_ms": result.processing_duration_ms,
        "status": "success"
        if isinstance(result, ChildAnalysisSuccess) and measurement.device_failure_reason is None
        else "failure",
        "device_requested": requested_device,
        "observed_execution_devices": measurement.observed_execution_devices,
        "device_failure_reason": measurement.device_failure_reason,
        "result_type": type(result).__name__,
        "torch_version": measurement.torch_version,
        "torchvision_version": measurement.torchvision_version,
        "torch_cuda_version": measurement.torch_cuda_version,
        "cuda_device_name": measurement.cuda_device_name,
        "cuda_peak_allocated_bytes": measurement.cuda_peak_allocated_bytes,
        "cuda_peak_reserved_bytes": measurement.cuda_peak_reserved_bytes,
        "sampled_memory": peaks.get(measurement.pid),
        "logs": _log_evidence(log_root, measurement.pid, request.analysis_id),
    }
    if isinstance(result, ChildAnalysisSuccess):
        record["response"] = json.loads(result.response_json)
        record["validated_response_type"] = type(validated).__name__
    elif isinstance(result, ChildAnalysisFailure):
        record["error_code"] = result.error_code
        record["failure_kind"] = result.failure_kind
    return record


def _versions() -> dict[str, str]:
    return {
        distribution.metadata["Name"]: distribution.version
        for distribution in distributions()
        if distribution.metadata.get("Name")
    }


def _finalize_run_status(report: dict[str, Any], *, stopped: bool, submitted_jobs: int) -> None:
    jobs = report["jobs"]
    expected = {
        f"bench-{report['stage_concurrency']}-{index}" for index in range(report["job_limit"])
    }
    report["computation_successful"] = (
        not stopped
        and submitted_jobs == report["job_limit"]
        and len(jobs) == report["job_limit"]
        and {job["analysis_id"] for job in jobs} == expected
        and all(job["status"] == "success" for job in jobs)
    )
    report["lifecycle_issues"] = _lifecycle_issues(report)
    report["lifecycle_valid"] = (
        report["computation_successful"]
        and report["stop_reason"] is None
        and not report["lifecycle_issues"]
    )
    report["completed"] = report["lifecycle_valid"]
    if not report["completed"] and report["stop_reason"] is None:
        report["stop_reason"] = "lifecycle_invalid"


def _run(args: argparse.Namespace) -> dict[str, Any]:
    settings = replace(
        Settings.from_environment(),
        model_device=args.device,
        debug_output_dir=str(args.output.parent / "debug"),
    )
    if settings.debug.enabled:
        raise ValueError("benchmark requires DEBUG_ARTIFACTS_ENABLED=false")
    video_root = Path(settings.video_storage_root).resolve(strict=True)
    videos = [Path(video_root / name).resolve(strict=True) for name in args.video]
    if any(path.parent != video_root or path.suffix.lower() != ".mp4" for path in videos):
        raise ValueError("videos must be .mp4 filenames in VIDEO_STORAGE_ROOT")
    model_paths = [
        Path(settings.model_path).resolve(strict=True),
        Path(settings.ball_model_path).resolve(strict=True),
    ]
    if any(not path.is_file() for path in model_paths):
        raise ValueError("model paths must be existing files")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    log_root = args.output.parent / (args.output.stem + "-logs")
    log_root.mkdir(exist_ok=False)
    config = asdict(settings)
    comparable = dict(config)
    comparable["model_device"] = "<DEVICE>"
    config_hash = hashlib.sha256(json.dumps(comparable, sort_keys=True).encode()).hexdigest()
    result: dict[str, Any] = {
        "schema_version": 1,
        "source_revision": args.source_revision,
        "image_identity": args.image_identity,
        "device_requested": args.device,
        "device_inventory": _device_inventory(),
        "stage_concurrency": args.workers,
        "job_limit": args.jobs,
        "deadline_seconds": args.max_seconds,
        "poll_wait_timeout_seconds": 0.5,
        "resource_sampling": "one sample per loop before polling; nvidia-smi can block up to 3 s per call, so cadence is not fixed; peaks are sampled lower bounds; CUDA allocator peaks are per child job",
        "resource_sample_offsets_ms": [],
        "submit_to_result_definition": "parent-observed latency from submission to result collection, including spawn, queueing, polling and resource sampling; not precise IPC time",
        "cuda_timing": "torch.cuda.synchronize before and after whole child analysis; existing stage logs are coarse host timings, not CUDA kernel timings",
        "settings": config,
        "comparable_settings_sha256": config_hash,
        "models": [
            {"name": path.name, "sha256": _sha256(path), "bytes": path.stat().st_size}
            for path in model_paths
        ],
        "videos": [_video_metadata(path) for path in videos],
        "dependencies": _versions(),
        "jobs": [],
        "computation_successful": False,
        "lifecycle_valid": False,
        "completed": False,
        "stop_reason": None,
        "unfinished_job_ids": [],
    }
    executor = ProcessPoolExecutor(
        max_workers=args.workers,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize,
        initargs=(settings, str(log_root)),
    )
    pending: dict[Future[ChildMeasurement], tuple[ChildAnalysisRequest, float]] = {}
    owned_workers: dict[int, BaseProcess] = {}
    peaks: dict[int, dict[str, int | None]] = {}
    parent_peak_rss_bytes = psutil.Process().memory_info().rss
    next_index = 0
    started = monotonic()
    stopped = False
    try:
        while next_index < args.jobs or pending:
            while not stopped and len(pending) < args.workers and next_index < args.jobs:
                video = videos[next_index % len(videos)]
                request = ChildAnalysisRequest(
                    f"bench-{args.workers}-{next_index}",
                    f"video-{next_index}",
                    "benchmark-player",
                    video.name,
                    analysis_version=settings.analysis_version,
                )
                submitted = perf_counter()
                pending[executor.submit(_analyze, request, args.device)] = (request, submitted)
                next_index += 1
                _track_workers(executor, owned_workers)
            _track_workers(executor, owned_workers)
            _sample_resources(peaks, owned_workers, args.device.startswith("cuda"))
            result["resource_sample_offsets_ms"].append(round((monotonic() - started) * 1000, 3))
            parent_peak_rss_bytes = max(parent_peak_rss_bytes, psutil.Process().memory_info().rss)
            if monotonic() - started >= args.max_seconds:
                result["stop_reason"] = "deadline_exceeded"
                stopped = True
                break
            done, _ = wait(pending, timeout=0.5)
            for future in done:
                request, submitted = pending.pop(future)
                try:
                    job = _job_record(
                        future.result(), request, args.device, submitted, peaks, log_root
                    )
                except Exception as error:
                    job = {
                        "analysis_id": request.analysis_id,
                        "status": "failure",
                        "device_requested": args.device,
                        "observed_execution_devices": None,
                        "error_type": type(error).__name__,
                    }
                    if isinstance(error, BenchmarkDeviceError):
                        job["error_message"] = str(error)
                result["jobs"].append(job)
                if job["status"] != "success":
                    result["stop_reason"] = "job_failed"
                    stopped = True
            if stopped:
                break
    except Exception as error:
        result["runner_error_type"] = type(error).__name__
        result["stop_reason"] = result["stop_reason"] or "runner_error"
        stopped = True
    finally:
        result["unfinished_job_ids"] = sorted(
            request.analysis_id for request, _ in pending.values()
        )
        for future in pending:
            try:
                future.cancel()
            except Exception as error:
                result.setdefault("cancellation_errors", []).append(type(error).__name__)
        try:
            _track_workers(executor, owned_workers)
        except Exception as error:
            result["worker_tracking_error"] = type(error).__name__
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception as error:
            result["executor_shutdown_error"] = type(error).__name__
        result["worker_cleanup"] = _cleanup_workers(owned_workers, stop_requested=stopped)
    _finalize_run_status(result, stopped=stopped, submitted_jobs=next_index)
    result["total_wall_ms"] = round((monotonic() - started) * 1000, 3)
    result["parent_peak_rss_bytes"] = parent_peak_rss_bytes
    result["sampled_child_memory"] = peaks
    result["child_pids"] = sorted({job["pid"] for job in result["jobs"] if "pid" in job})
    result["model_reuse_observed"] = (
        args.workers == 1
        and len(result["child_pids"]) == 1
        and len(result["jobs"]) > 1
        and all(job["status"] == "success" for job in result["jobs"])
        and len(result["jobs"][-1]["logs"]["player_model_load_lines"]) == 1
        and len(result["jobs"][-1]["logs"]["ball_model_load_lines"]) == 1
    )
    return result


_MISSING = object()


def _differences(left: Any, right: Any, path: str = "$") -> list[dict[str, Any]]:
    if isinstance(left, dict) and isinstance(right, dict):
        return [
            difference
            for key in sorted(left.keys() | right.keys())
            for difference in _differences(
                left.get(key, _MISSING), right.get(key, _MISSING), f"{path}.{key}"
            )
        ]
    if isinstance(left, list) and isinstance(right, list):
        return [
            difference
            for index in range(max(len(left), len(right)))
            for difference in _differences(
                left[index] if index < len(left) else _MISSING,
                right[index] if index < len(right) else _MISSING,
                f"{path}[{index}]",
            )
        ]
    if (
        left is not _MISSING
        and right is not _MISSING
        and left == right
        and type(left) is type(right)
    ):
        return []
    return [
        {
            "path": path,
            "cpu": "<MISSING>" if left is _MISSING else left,
            "gpu": "<MISSING>" if right is _MISSING else right,
            "kind": "numeric"
            if type(left) in (int, float) and type(right) in (int, float)
            else "categorical",
        }
    ]


def _run_evidence_issues(report: dict[str, Any], device: str) -> list[str]:
    issues: list[str] = []
    if report.get("computation_successful") is not True:
        issues.append("computation_incomplete")
    if (
        report.get("lifecycle_valid") is not True
        or report.get("completed") is not True
        or report.get("stop_reason") is not None
    ):
        issues.append("run_incomplete")
    issues.extend(_lifecycle_issues(report))
    if report.get("device_requested") != device:
        issues.append("requested_device_mismatch")
    workers, limit = report.get("stage_concurrency"), report.get("job_limit")
    jobs = report.get("jobs")
    caps = {1: 2, 2: 4, 4: 8}
    if (
        not isinstance(workers, int)
        or workers not in caps
        or not isinstance(limit, int)
        or not 1 <= limit <= caps[workers]
        or not isinstance(jobs, list)
    ):
        return [*issues, "invalid_job_inventory"]
    expected = {f"bench-{workers}-{index}" for index in range(limit)}
    raw_actual = [job.get("analysis_id") for job in jobs if isinstance(job, dict)]
    if any(not isinstance(value, str) for value in raw_actual):
        issues.append("invalid_job_id")
    actual = [str(value) for value in raw_actual]
    if len(actual) != len(jobs) or set(actual) != expected or len(actual) != len(expected):
        issues.append("expected_unique_jobs_missing_or_extra")
    if any(count > 1 for count in Counter(actual).values()):
        issues.append("duplicate_job_ids")
    for job in jobs:
        if not isinstance(job, dict):
            issues.append("invalid_job_record")
            continue
        analysis_id = str(job.get("analysis_id"))
        if job.get("status") != "success" or not isinstance(job.get("response"), dict):
            issues.append(f"{analysis_id}:unsuccessful_or_missing_response")
        if job.get("device_requested") != device:
            issues.append(f"{analysis_id}:requested_device_mismatch")
        if job.get("device_failure_reason") is not None:
            issues.append(f"{analysis_id}:device_failure_reported")
        observed = job.get("observed_execution_devices")
        try:
            if not isinstance(observed, dict):
                raise KeyError("observed_execution_devices")
            reason = _device_failure_reason(device, observed)
        except (KeyError, TypeError, AttributeError):
            reason = "execution_device_evidence_missing"
        if reason is not None:
            issues.append(f"{analysis_id}:{reason}")
    return issues


def _compare(cpu_path: Path, gpu_path: Path) -> dict[str, Any]:
    cpu = json.loads(cpu_path.read_text(encoding="utf-8"))
    gpu = json.loads(gpu_path.read_text(encoding="utf-8"))
    comparable_keys = (
        "source_revision",
        "comparable_settings_sha256",
        "models",
        "videos",
        "stage_concurrency",
        "job_limit",
    )
    mismatches = {
        key: {"cpu": cpu.get(key), "gpu": gpu.get(key)}
        for key in comparable_keys
        if cpu.get(key) != gpu.get(key)
    }
    if cpu.get("device_requested") != "cpu" or gpu.get("device_requested") != "cuda:0":
        mismatches["devices"] = {
            "cpu": cpu.get("device_requested"),
            "gpu": gpu.get("device_requested"),
        }
    responses = []
    cpu_jobs = {
        job["analysis_id"]: job
        for job in cpu.get("jobs", [])
        if isinstance(job, dict) and isinstance(job.get("analysis_id"), str)
    }
    gpu_jobs = {
        job["analysis_id"]: job
        for job in gpu.get("jobs", [])
        if isinstance(job, dict) and isinstance(job.get("analysis_id"), str)
    }
    evidence_issues = {
        "cpu": _run_evidence_issues(cpu, "cpu"),
        "gpu": _run_evidence_issues(gpu, "cuda:0"),
    }
    for analysis_id in sorted(cpu_jobs.keys() | gpu_jobs.keys()):
        left = cpu_jobs.get(analysis_id, {})
        right = gpu_jobs.get(analysis_id, {})
        responses.append(
            {
                "analysis_id": analysis_id,
                "cpu_status": left.get("status"),
                "gpu_status": right.get("status"),
                "differences": _differences(left.get("response"), right.get("response")),
            }
        )
    return {
        "inputs_compatible": not mismatches,
        "comparable": not mismatches,
        "performance_comparison_valid": not mismatches and not any(evidence_issues.values()),
        "run_evidence_issues": evidence_issues,
        "input_mismatches": mismatches,
        "result_comparisons": responses,
        "diagnostic_jobs": {"cpu": cpu.get("jobs", []), "gpu": gpu.get("jobs", [])},
        "cpu_job_count": len(cpu.get("jobs", [])),
        "gpu_job_count": len(gpu.get("jobs", [])),
        "tolerance": None,
        "note": "Input compatibility and run validity are separate. All exact numerical and categorical response differences are retained; no tolerance has been approved.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("CPU_JSON", "GPU_JSON"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--video", action="append", default=[])
    parser.add_argument("--device", choices=("cpu", "cuda:0"))
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=1)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--allow-concurrency", action="store_true")
    parser.add_argument("--source-revision")
    parser.add_argument("--image-identity")
    args = parser.parse_args()
    if args.compare:
        payload = _compare(*args.compare)
    else:
        caps = {1: 2, 2: 4, 4: 8}
        if not args.video or not args.device or not args.source_revision or not args.image_identity:
            parser.error("run requires --video, --device, --source-revision, and --image-identity")
        if not 1 <= args.jobs <= caps[args.workers] or not 1 <= args.max_seconds <= 900:
            parser.error("job count or deadline exceeds the stage cap")
        if args.workers > 1 and not args.allow_concurrency:
            parser.error("stages 2 and 4 require --allow-concurrency")
        payload = _run(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
    )
    if (args.compare and not payload["performance_comparison_valid"]) or (
        not args.compare and not payload["completed"]
    ):
        sys.exit(1)


if __name__ == "__main__":
    main()
