# Concurrency-one baseline runbook

## Purpose and limits

Capture a controlled MVP baseline with exactly one active analysis before any concurrency-two trial. This runbook measures current in-process behavior; it does not establish durable acceptance, restart recovery, or exactly-once execution. Do not run a concurrency-two test in MVP-1 and do not change production configuration without separate authorization.

## Log observations

The service emits concise stdlib log events without callback URLs, video references, payloads, temporary paths, or credentials:

| Event | Key fields |
|---|---|
| `analysis_admission_accepted` / `analysis_admission_rejected` | `admission_duration_ms`, queue depth/capacity, active count/limit, accepting state; rejection reason. |
| `analysis_job_started` | `queue_wait_ms` and queue/active snapshot. |
| `analysis_execution_finished` | `analysis_duration_ms`; this stops before inline callback delivery. |
| `analysis_callback_attempt_finished`, `analysis_callback_retry_scheduled`, `analysis_callback_finished` | attempt/maximum, attempt and total callback duration, status class where available, retry delay, delivered/exhausted outcome. |
| `analysis_cleanup_finished` | cleanup duration, completion/failed/cancelled path, success flag, error count. |
| `analysis_job_terminal` | final in-memory state and `end_to_end_duration_ms`, measured from successful enqueue through the inline callback path. |
| `analysis_shutdown_started`, `analysis_job_cancelled`, `analysis_shutdown_finished` | shutdown and queued-job cancellation evidence. |

`queue_wait_ms` starts after successful enqueue and ends when the worker marks a job running. `analysis_duration_ms` excludes queue wait and callback delivery. `callback_duration_ms` includes all current retries and retry delays. `end_to_end_duration_ms` includes queue wait, execution, and the current inline callback path.

## Run with one active analysis

Verify the checked-out configuration still contains `DEFAULT_MAX_ACTIVE_ANALYSES = 1`, then start the normal service through the approved deployment or local workflow. Do not alter queue capacity, worker count, or callback configuration for the baseline.

For a local non-production check:

```powershell
uv run uvicorn main:app --host 127.0.0.1 --port 8000
```

Use only approved test videos and a callback receiver that does not expose credentials in request or response logging.

## Capture procedure

1. Record process/container start and the first model-load observations.
2. Submit one valid request and collect its admission, start, execution, callback, cleanup, and terminal events.
3. Submit two valid requests close together. Confirm the second has queue wait while the first runs; do not submit enough work to change queue configuration.
4. Exercise separately: queue saturation, invalid video, analysis exception, callback timeout/failure, shutdown while queued, and shutdown while running.
5. Preserve UTC capture time, build/commit identifier, model paths by approved version label only, test-video label, and the log event stream. Do not place raw URLs, filesystem paths, callback bodies, tokens, or credentials in shared evidence.

## Host and container observations

Run these only on an authorized environment and retain timestamped output alongside application logs:

```bash
docker compose ps
docker stats --no-stream
docker compose logs --timestamps football-analysis
free -h
uptime
ps -eo pid,ppid,%cpu,%mem,nlwp,cmd --sort=-%cpu
dmesg --ctime | tail -n 100
```

Use `docker inspect` or platform logs to identify container restarts/OOM evidence. Capture peak and steady container/host RAM, swap usage, CPU/load, process thread count, callback latency/failures, API responsiveness, and temporary-resource cleanup outcome. The existing performance collector can provide stage/resource snapshots when explicitly enabled by its benchmark caller; it does not replace the lifecycle log boundaries above.

## Evidence review

For each job, correlate by `analysis_id` and verify the ordering: accepted, started, execution finished, callback finished (unless cancellation prevents it), cleanup finished, terminal. A failed callback does not make a completed analysis failed. Treat missing terminal evidence during restart tests as expected non-durability, not proof of recovery. Share only sanitized summaries and raw logs with protected identifiers where operational policy requires it.
