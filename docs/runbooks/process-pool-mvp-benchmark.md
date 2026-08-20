# One-process ProcessPool MVP smoke and benchmark

Run only on local, test, or staging. Do not use this procedure against VPS production without explicit authorization. The current runtime is intentionally one `AnalysisWorker` and one spawned `ProcessPoolExecutor` child (`max_workers=1`); these scenarios do not measure concurrent analysis.

## Prerequisites

- Python environment installed with `uv sync` already completed; do not download a different model for this run.
- A readable `VIDEO_STORAGE_ROOT` containing the approved `.mp4` references used below.
- The configured `MODEL_PATH` available to the process child, and enough disk for `DEBUG_OUTPUT_DIR` when debug artifacts are enabled.
- The in-process repository backend mock is suitable for automated integration tests, not as a live callback URL. A live smoke run needs an approved staging Apex callback endpoint or an explicitly approved public ingress in front of a test receiver. Its hostname must resolve only to global/public IPs. Do not disable or bypass CallbackService SSRF validation, expose a receiver publicly without authorization, or invent callback credentials/signatures beyond the current integration contract.
- Record machine/container details, CPU count, RAM, OS, image/commit, model file checksum/version, and all environment values used (excluding secrets).

Required settings include `VIDEO_STORAGE_ROOT`, `MODEL_PATH`, `MODEL_DEVICE`, `DEBUG_OUTPUT_DIR`, `MAX_QUEUED_ANALYSES`, `CALLBACK_TIMEOUT_SECONDS`, and any analysis/version settings from `.env.example`. Keep `MAX_QUEUED_ANALYSES` explicit. Do not increase process or worker count.

## Start and verify

```powershell
$env:VIDEO_STORAGE_ROOT = 'E:\staging-videos'
$env:MODEL_PATH = 'E:\super7\yolo11n.pt'
$env:DEBUG_OUTPUT_DIR = 'E:\super7\debug'
$env:MAX_QUEUED_ANALYSES = '5'
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

In another terminal, confirm readiness before submitting work:

```powershell
Invoke-WebRequest http://127.0.0.1:8000/health/live
Invoke-WebRequest http://127.0.0.1:8000/health/ready
```

This command requires `uv sync` and the editable project installation first. For Docker/VPS startup, use the repository's existing Docker Compose/start command rather than a second deployment method.

Submit only safe video references (filenames, not paths) and record each `analysisId` from HTTP 202. Replace the placeholder callback URL with the approved staging endpoint:

```powershell
$body = @{ videoId='video-001'; playerId='player-001'; videoUrl='known.mp4'; callbackUrl='https://APPROVED-STAGING-CALLBACK.example/webhook' } | ConvertTo-Json
Measure-Command { Invoke-RestMethod http://127.0.0.1:8000/analyze -Method Post -ContentType 'application/json' -Body $body }
```

Use the callback receiver/backend mock to verify exactly one callback per completed or failed job and matching callback `request_id`/analysis ID. Cancellation currently sends no callback.

## Scenarios

### A. Cold single request

Start a fresh application, submit one known video, and retain parent and child logs. Capture first child model-load effect, initialized child PID (must differ from parent PID), `analysis_duration_ms`, `end_to_end_duration_ms`, callback result, and health latency while work is active. Record disk/artifact directories before and after.

### B. Warm sequential requests

Submit three known requests only after the prior terminal callback. Record `analysis_child_initialized child_pid=<pid>`, observe that the same OS child remains alive across all three jobs, confirm no second initialization and no `BrokenProcessPool`/restart event, and then infer that the retained child handled the sequential jobs because `max_workers=1`. This is process-lifecycle evidence, not direct per-job PID attribution. Verify every job has one terminal state, one callback per completed/failed job, and request artifact directories are cleaned. Compare cold and warm latency; do not infer a target threshold.

### C. Admission burst with one process

Submit five requests quickly and record `MAX_QUEUED_ANALYSES`. One request may become active while the remaining requests occupy the bounded queue; scheduling means there is no guaranteed rejection count. Record actual admitted/rejected counts and each HTTP admission response. Verify serialized execution (not concurrency), FIFO callback order where all jobs are admitted, and each admitted job's queue wait. Probe both health endpoints during the burst: `/health/live` should remain responsive; `/health/ready` may intentionally return 503 when capacity is exhausted or shutdown begins. Record readiness status and latency, and do not classify an expected capacity 503 as an application crash.

## Measurements and evidence

Collect existing structured log events: `analysis_admission_accepted`/`rejected`, `analysis_job_started` (`queue_wait_ms`), `analysis_execution_finished` (`analysis_duration_ms`), `analysis_job_terminal` (`end_to_end_duration_ms`), callback attempt/finished events (`callback_duration_ms`, attempts, outcome), and `analysis_child_initialized` (child PID). Never put callback URLs, paths, or secrets in benchmark notes.

Use OS tools separately for CPU, peak RAM, and disk usage. On Windows, capture `Get-Process python | Select-Object Id,CPU,WorkingSet64` periodically and `Get-ChildItem $env:DEBUG_OUTPUT_DIR`; on Linux use `ps`, `top`/`pidstat`, and `du`. Capture parent PID from the server process and child PID from child initialization logs.

## Graceful shutdown and cleanup

Stop submitting work, record queued/active IDs, then send Ctrl+C to the server. The application closes admission, cancels queued/parent waiting work, and shuts down the pool. A running real child analysis may finish before pool shutdown returns; parent cancellation does not force-terminate it. After exit, verify no server/child process remains, callback receiver has expected events, and artifact directories match retention policy. Remove only run-specific debug artifacts after recording before/after disk usage.

## Results template

| Environment | Scenario | Requests | Admitted / rejected | Cold/warm | Admission p50 / max | Queue wait | Analysis duration | End-to-end | Peak CPU / RAM | Callbacks delivered | Errors | Notes |
|---|---:|---:|---|---|---|---|---|---|---|---|---|---|
| not measured | cold single | 1 | not measured | cold | not measured | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| not measured | warm sequential | 3 | not measured | warm | not measured | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
| not measured | admission burst | 5 | not measured | warm | not measured | not measured | not measured | not measured | not measured | not measured | not measured | not measured |
