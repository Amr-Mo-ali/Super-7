"""The benchmark comparison must retain response differences and input mismatches."""

import json
import multiprocessing
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from multiprocessing.process import BaseProcess
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts import benchmark_gpu_pipeline as benchmark
from scripts.benchmark_gpu_pipeline import _compare, _differences
from services.process_contracts import ChildAnalysisRequest, ChildAnalysisSuccess


def _sleeping_worker(seconds: float = 30) -> BaseProcess:
    process = multiprocessing.get_context("spawn").Process(target=time.sleep, args=(seconds,))
    process.start()
    return process


def _reap_test_workers(*processes: BaseProcess) -> None:
    for process in processes:
        if process.is_alive():
            process.kill()
        process.join(timeout=2)


def _pid(process: BaseProcess) -> int:
    assert process.pid is not None
    return process.pid


def _normal_cleanup() -> dict[str, Any]:
    return {
        "cleanup_verified": True,
        "unresolved_processes": [],
        "forced_termination_pids": [],
        "workers": [
            {
                "pid": 123,
                "exit_verified": True,
                "exitcode": 0,
                "terminate_requested": False,
                "kill_requested": False,
                "errors": [],
            }
        ],
    }


def test_worker_registry_tracks_only_its_executor_process() -> None:
    unrelated = _sleeping_worker()
    try:
        with ProcessPoolExecutor(
            max_workers=1, mp_context=multiprocessing.get_context("spawn")
        ) as executor:
            worker_pid = executor.submit(os.getpid).result(timeout=10)
            owned: dict[int, BaseProcess] = {}
            benchmark._track_workers(executor, owned)
            assert set(owned) == {worker_pid}
        assert unrelated.is_alive()
    finally:
        _reap_test_workers(unrelated)


def test_cleanup_verifies_successful_exit_and_ignores_unowned_child() -> None:
    owned = _sleeping_worker(0.1)
    unrelated = _sleeping_worker()
    try:
        report = benchmark._cleanup_workers(
            {_pid(owned): owned}, stop_requested=False, grace_seconds=5
        )
        assert report["cleanup_verified"] is True
        assert report["unresolved_processes"] == []
        assert report["workers"][0]["exit_verified"] is True
        assert report["workers"][0]["exitcode"] == 0
        assert benchmark._lifecycle_issues({"worker_cleanup": report}) == []
        assert unrelated.is_alive()
    finally:
        _reap_test_workers(owned, unrelated)


def test_hung_child_escalates_to_kill(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _sleeping_worker()
    try:
        monkeypatch.setattr(process, "terminate", lambda: None)
        report = benchmark._cleanup_workers(
            {_pid(process): process}, stop_requested=True, grace_seconds=0.1
        )
        assert report["forced_termination_pids"] == [_pid(process)]
        assert report["cleanup_verified"] is True
        assert any(
            "shutdown_escalated" in issue
            for issue in benchmark._lifecycle_issues({"worker_cleanup": report})
        )
        assert not process.is_alive()

        response = {"score": 0.75}
        run: dict[str, Any] = {
            "stage_concurrency": 1,
            "job_limit": 1,
            "jobs": [{"analysis_id": "bench-1-0", "status": "success", "response": response}],
            "worker_cleanup": report,
            "stop_reason": None,
        }
        benchmark._finalize_run_status(run, stopped=False, submitted_jobs=1)
        assert run["computation_successful"] is True
        assert run["jobs"][0]["response"] == response
        assert run["lifecycle_valid"] is False
        assert run["completed"] is False
    finally:
        _reap_test_workers(process)


def test_termination_failure_still_cleans_every_owned_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = _sleeping_worker()
    other = _sleeping_worker()
    try:

        def fail_terminate() -> None:
            raise OSError("simulated termination failure")

        monkeypatch.setattr(failing, "terminate", fail_terminate)
        report = benchmark._cleanup_workers(
            {_pid(failing): failing, _pid(other): other},
            stop_requested=True,
            grace_seconds=0.1,
        )
        assert report["cleanup_verified"] is True
        assert _pid(failing) in report["forced_termination_pids"]
        assert {entry["pid"] for entry in report["workers"]} == {_pid(failing), _pid(other)}
        assert any("terminate: OSError" in error for error in report["workers"][0]["errors"])
        assert not failing.is_alive() and not other.is_alive()
    finally:
        _reap_test_workers(failing, other)


def test_liveness_inspection_failure_is_not_reported_as_cleanup_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _sleeping_worker()
    try:
        original = process.is_alive
        inspections = 0

        def flaky_liveness() -> bool:
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                raise OSError("simulated inspection failure")
            return original()

        monkeypatch.setattr(process, "is_alive", flaky_liveness)
        report = benchmark._cleanup_workers(
            {_pid(process): process}, stop_requested=True, grace_seconds=0.1
        )
        assert report["cleanup_verified"] is False
        assert report["unresolved_processes"] == [_pid(process)]
        assert not original()
    finally:
        _reap_test_workers(process)


@pytest.mark.parametrize(
    ("exitcode", "terminate_requested", "errors", "expected_issue"),
    [
        (1, False, [], "exitcode_nonzero_or_unavailable"),
        (None, False, [], "exitcode_nonzero_or_unavailable"),
        (0, True, [], "shutdown_escalated"),
        (0, False, ["join: OSError"], "cleanup_errors"),
    ],
)
def test_computation_success_does_not_validate_bad_worker_lifecycle(
    exitcode: int | None,
    terminate_requested: bool,
    errors: list[str],
    expected_issue: str,
) -> None:
    report: dict[str, Any] = {
        "stage_concurrency": 1,
        "job_limit": 1,
        "jobs": [{"analysis_id": "bench-1-0", "status": "success", "response": {"value": 1}}],
        "worker_cleanup": _normal_cleanup(),
        "stop_reason": None,
    }
    worker = report["worker_cleanup"]["workers"][0]
    worker["exitcode"] = exitcode
    worker["terminate_requested"] = terminate_requested
    worker["errors"] = errors

    benchmark._finalize_run_status(report, stopped=False, submitted_jobs=1)

    assert report["computation_successful"] is True
    assert report["lifecycle_valid"] is False
    assert report["completed"] is False
    assert any(expected_issue in issue for issue in report["lifecycle_issues"])
    assert report["jobs"][0]["response"] == {"value": 1}


def test_inference_hook_observes_actual_input_and_restores_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ultralytics.engine.predictor import BasePredictor

    def original(_predictor: object, _image: object) -> str:
        return "inferred"

    monkeypatch.setattr(BasePredictor, "inference", original)
    predictor = object()
    with benchmark._capture_inference_devices() as observations:
        assert (
            cast(Any, BasePredictor.inference)(predictor, SimpleNamespace(device="cpu"))
            == "inferred"
        )
    assert observations[id(predictor)] == {"cpu"}
    assert BasePredictor.inference is original


def test_gpu_request_fails_when_cuda_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "torchvision", SimpleNamespace())
    request = ChildAnalysisRequest("bench-1-0", "video-0", "benchmark-player", "video.mp4")

    with pytest.raises(benchmark.BenchmarkDeviceError, match="CUDA requested"):
        benchmark._analyze(request, "cuda:0")


def test_gpu_device_evidence_rejects_cpu_fallback_and_missing_inference() -> None:
    observed = {
        "player": {"inference_input_devices": ["cuda:0"], "model_backend_device": "cuda:0"},
        "ball": {"inference_input_devices": ["cpu"], "model_backend_device": "cpu"},
    }
    reason = benchmark._device_failure_reason("cuda:0", observed)
    assert reason is not None and "ball execution device unverified" in reason
    observed["ball"] = {"inference_input_devices": [], "model_backend_device": "cuda:0"}
    reason = benchmark._device_failure_reason("cuda:0", observed)
    assert reason is not None and "ball execution device unverified" in reason
    observed["ball"]["inference_input_devices"] = ["cuda:0"]
    assert benchmark._device_failure_reason("cuda:0", observed) is None


def test_gpu_analysis_records_cpu_fallback_as_failure_without_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cuda = SimpleNamespace(
        is_available=lambda: True,
        synchronize=lambda _: None,
        reset_peak_memory_stats=lambda _: None,
        get_device_name=lambda _: "fake GPU",
        max_memory_allocated=lambda _: 0,
        max_memory_reserved=lambda _: 0,
    )
    monkeypatch.setitem(
        sys.modules,
        "torch",
        SimpleNamespace(cuda=cuda, version=SimpleNamespace(cuda="12.6"), __version__="test"),
    )
    monkeypatch.setitem(sys.modules, "torchvision", SimpleNamespace(__version__="test"))
    observed = {
        "player": {"inference_input_devices": ["cpu"], "model_backend_device": "cpu"},
        "ball": {"inference_input_devices": ["cpu"], "model_backend_device": "cpu"},
    }
    monkeypatch.setattr(benchmark, "_capture_inference_devices", lambda: nullcontext({}))
    monkeypatch.setattr(benchmark, "_observed_execution_devices", lambda _: observed)
    monkeypatch.setattr(benchmark, "run_child_analysis", lambda _: object())
    request = ChildAnalysisRequest("bench-1-0", "video-0", "benchmark-player", "video.mp4")

    measurement = benchmark._analyze(request, "cuda:0")

    assert measurement.observed_execution_devices == observed
    assert measurement.device_failure_reason is not None
    assert "player execution device unverified" in measurement.device_failure_reason


def test_successful_analysis_with_cpu_fallback_is_failed_gpu_job(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(benchmark, "validate_child_result", lambda *_: object())
    monkeypatch.setattr(benchmark, "_log_evidence", lambda *_: {})
    request = ChildAnalysisRequest("bench-1-0", "video-0", "benchmark-player", "video.mp4")
    response = ChildAnalysisSuccess("bench-1-0", "{}", "v1", "model", 1)
    observed = {
        "player": {"inference_input_devices": ["cuda:0"], "model_backend_device": "cuda:0"},
        "ball": {"inference_input_devices": ["cpu"], "model_backend_device": "cpu"},
    }
    measurement = benchmark.ChildMeasurement(
        123,
        2.0,
        response,
        "torch",
        "vision",
        "12.6",
        "fake GPU",
        0,
        0,
        observed,
        benchmark._device_failure_reason("cuda:0", observed),
    )

    job = benchmark._job_record(measurement, request, "cuda:0", 0.0, {}, tmp_path)

    assert job["status"] == "failure"
    assert job["device_requested"] == "cuda:0"
    assert job["observed_execution_devices"]["ball"]["inference_input_devices"] == ["cpu"]


def test_comparison_preserves_numerical_categorical_and_missing_values() -> None:
    differences = _differences(
        {"score": 3.0, "status": "available", "null_value": None},
        {"score": 3.1, "status": "unavailable", "added": None},
    )

    assert differences == [
        {"path": "$.added", "cpu": "<MISSING>", "gpu": None, "kind": "categorical"},
        {"path": "$.null_value", "cpu": None, "gpu": "<MISSING>", "kind": "categorical"},
        {"path": "$.score", "cpu": 3.0, "gpu": 3.1, "kind": "numeric"},
        {"path": "$.status", "cpu": "available", "gpu": "unavailable", "kind": "categorical"},
    ]


def test_comparison_matches_jobs_by_identity_and_checks_input_hashes(tmp_path: Path) -> None:
    common = {
        "source_revision": "base",
        "comparable_settings_sha256": "settings",
        "models": [{"sha256": "weight"}],
        "videos": [{"sha256": "video"}],
        "stage_concurrency": 1,
        "job_limit": 2,
        "computation_successful": True,
        "lifecycle_valid": True,
        "completed": True,
        "stop_reason": None,
        "worker_cleanup": _normal_cleanup(),
    }

    def job(index: int, value: int, device: str) -> dict[str, object]:
        evidence = {
            name: {"inference_input_devices": [device], "model_backend_device": device}
            for name in ("player", "ball")
        }
        return {
            "analysis_id": f"bench-1-{index}",
            "status": "success",
            "response": {"value": value},
            "device_requested": device,
            "observed_execution_devices": evidence,
            "device_failure_reason": None,
        }

    cpu = {
        **common,
        "device_requested": "cpu",
        "jobs": [job(1, 2, "cpu"), job(0, 1, "cpu")],
    }
    gpu = {
        **common,
        "device_requested": "cuda:0",
        "jobs": [job(0, 1, "cuda:0"), job(1, 3, "cuda:0")],
    }
    cpu_path = tmp_path / "cpu.json"
    gpu_path = tmp_path / "gpu.json"
    cpu_path.write_text(json.dumps(cpu), encoding="utf-8")
    gpu_path.write_text(json.dumps(gpu), encoding="utf-8")

    result = _compare(cpu_path, gpu_path)

    assert result["comparable"] is True
    assert result["inputs_compatible"] is True
    assert result["performance_comparison_valid"] is True
    assert result["result_comparisons"][0]["differences"] == []
    assert result["result_comparisons"][1]["differences"] == [
        {"path": "$.value", "cpu": 2, "gpu": 3, "kind": "numeric"}
    ]

    cpu_missing = json.loads(cpu_path.read_text(encoding="utf-8"))
    cpu_missing["jobs"][0]["observed_execution_devices"]["ball"]["inference_input_devices"] = []
    cpu_path.write_text(json.dumps(cpu_missing), encoding="utf-8")
    missing_comparison = _compare(cpu_path, gpu_path)
    assert missing_comparison["inputs_compatible"] is True
    assert missing_comparison["performance_comparison_valid"] is False
    assert any(
        "ball execution device unverified" in issue
        for issue in missing_comparison["run_evidence_issues"]["cpu"]
    )

    cpu_missing["jobs"][0]["observed_execution_devices"]["ball"] = {
        "inference_input_devices": ["cuda:0"],
        "model_backend_device": "cuda:0",
    }
    cpu_path.write_text(json.dumps(cpu_missing), encoding="utf-8")
    assert _compare(cpu_path, gpu_path)["performance_comparison_valid"] is False
    cpu_path.write_text(json.dumps(cpu), encoding="utf-8")

    cpu_escalated = json.loads(cpu_path.read_text(encoding="utf-8"))
    cpu_escalated["worker_cleanup"]["workers"][0]["terminate_requested"] = True
    cpu_path.write_text(json.dumps(cpu_escalated), encoding="utf-8")
    escalated_comparison = _compare(cpu_path, gpu_path)
    assert escalated_comparison["inputs_compatible"] is True
    assert escalated_comparison["performance_comparison_valid"] is False
    assert any(
        "shutdown_escalated" in issue
        for issue in escalated_comparison["run_evidence_issues"]["cpu"]
    )
    cpu_path.write_text(json.dumps(cpu), encoding="utf-8")

    gpu["models"] = [{"sha256": "different-weight"}]
    gpu_path.write_text(json.dumps(gpu), encoding="utf-8")
    assert _compare(cpu_path, gpu_path)["input_mismatches"]["models"] == {
        "cpu": [{"sha256": "weight"}],
        "gpu": [{"sha256": "different-weight"}],
    }


def test_comparison_keeps_partial_and_failed_jobs_but_rejects_performance_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    common = {
        "source_revision": "base",
        "comparable_settings_sha256": "settings",
        "models": [{"sha256": "weight"}],
        "videos": [{"sha256": "video"}],
        "stage_concurrency": 1,
        "job_limit": 2,
        "worker_cleanup": _normal_cleanup(),
    }
    cpu_job = {
        "analysis_id": "bench-1-0",
        "status": "success",
        "response": {"value": 1},
        "device_requested": "cpu",
        "observed_execution_devices": {
            name: {"inference_input_devices": ["cpu"], "model_backend_device": "cpu"}
            for name in ("player", "ball")
        },
    }
    gpu_job = {
        **cpu_job,
        "device_requested": "cuda:0",
        "observed_execution_devices": {
            name: {"inference_input_devices": ["cpu"], "model_backend_device": "cpu"}
            for name in ("player", "ball")
        },
    }
    cpu = {
        **common,
        "device_requested": "cpu",
        "computation_successful": False,
        "lifecycle_valid": False,
        "completed": False,
        "stop_reason": "deadline_exceeded",
        "jobs": [cpu_job],
    }
    gpu = {
        **common,
        "device_requested": "cuda:0",
        "computation_successful": True,
        "lifecycle_valid": True,
        "completed": True,
        "stop_reason": None,
        "jobs": [gpu_job, gpu_job],
    }
    cpu_path, gpu_path = tmp_path / "cpu.json", tmp_path / "gpu.json"
    cpu_path.write_text(json.dumps(cpu), encoding="utf-8")
    gpu_path.write_text(json.dumps(gpu), encoding="utf-8")

    comparison = _compare(cpu_path, gpu_path)

    assert comparison["inputs_compatible"] is True
    assert comparison["performance_comparison_valid"] is False
    assert "run_incomplete" in comparison["run_evidence_issues"]["cpu"]
    assert "duplicate_job_ids" in comparison["run_evidence_issues"]["gpu"]
    assert any(
        "execution device unverified" in issue for issue in comparison["run_evidence_issues"]["gpu"]
    )
    assert comparison["diagnostic_jobs"] == {"cpu": [cpu_job], "gpu": [gpu_job, gpu_job]}

    output = tmp_path / "comparison.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark", "--compare", str(cpu_path), str(gpu_path), "--output", str(output)],
    )
    with pytest.raises(SystemExit) as exited:
        benchmark.main()
    assert exited.value.code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["diagnostic_jobs"]["gpu"] == [
        gpu_job,
        gpu_job,
    ]


def test_cpu_execution_evidence_must_match_cpu_request() -> None:
    report = {
        "computation_successful": True,
        "lifecycle_valid": True,
        "completed": True,
        "stop_reason": None,
        "worker_cleanup": _normal_cleanup(),
        "device_requested": "cpu",
        "stage_concurrency": 1,
        "job_limit": 1,
        "jobs": [
            {
                "analysis_id": "bench-1-0",
                "status": "success",
                "response": {},
                "device_requested": "cpu",
                "observed_execution_devices": {
                    name: {
                        "inference_input_devices": ["cuda:0"],
                        "model_backend_device": "cuda:0",
                    }
                    for name in ("player", "ball")
                },
            }
        ],
    }

    assert any(
        "execution device unverified" in issue
        for issue in benchmark._run_evidence_issues(report, "cpu")
    )
