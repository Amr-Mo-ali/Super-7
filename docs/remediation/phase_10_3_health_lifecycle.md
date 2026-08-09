# Phase 10.3 — Health and lifecycle readiness

## 1. Objective

Provide the minimal production health and lifecycle contract required by the deployment audit, without changing Public Rating V2, analysis algorithms, model ownership, admission capacity, or concurrency topology.

## 2. Blockers addressed

| Blocker | Resolution |
| --- | --- |
| No liveness endpoint | Added `GET /health/live`. |
| No readiness endpoint | Added `GET /health/ready`. |
| No combined health endpoint | Added `GET /health`. |
| Shutdown did not stop admissions or cancel active requests | `RequestLifecycle.shutdown()` closes the existing admission controller, signals active request cancellation managers, and waits for their existing cleanup paths. FastAPI lifespan shutdown calls it. |
| Deadline state existed but was not registered per request | `RequestLifecycle` now creates one request-local deadline task that calls the existing `CancellationManager.expire_deadline()` method. |

## 3. Endpoints added

| Endpoint | Meaning | Success response |
| --- | --- | --- |
| `GET /health/live` | The application composition completed. | 200 when startup, detector initialization, and configuration-loading state are all true. |
| `GET /health/ready` | The process can accept a new analysis. | 200 only when admission is accepting and has capacity, the lifecycle is not shutting down, an artifact manager exists, initialized models are available, and the system temp upload directory is writable. |
| `GET /health` | Combined liveness and readiness view. | 200 only when both component check groups pass. |

All endpoints return a small operational JSON object with `status`, `component`, and `checks`. They perform no inference, video decoding, artifact creation, or model reload. A failed check returns HTTP 503.

## 4. Lifecycle changes

`RequestLifecycle` remains the owner of request cancellation, artifacts, executor invocation, and admission permits. It now also keeps an in-memory, process-local map of active request cancellation managers and completion events.

For every admitted invocation, the lifecycle:

```text
admission permit
  -> request-local CancellationManager registration
  -> request-local deadline task registration (when configured)
  -> existing thread executor and synchronous pipeline
  -> existing artifact cleanup (when present)
  -> deadline task cancellation
  -> cancellation completion
  -> permit release
  -> active-request completion signal
```

The completion signal occurs only after cleanup and permit release. This lets shutdown wait for resource ownership to return through the same `finally` path used by ordinary request completion and failures.

## 5. Shutdown behavior

FastAPI's lifespan handler calls `RequestLifecycle.shutdown()` when the server enters shutdown.

```text
server shutdown / lifespan exit
  -> mark lifecycle shutting down
  -> close admission controller (new work is rejected)
  -> request shutdown cancellation for each active request
  -> active pipeline reaches an existing cooperative cancellation checkpoint
  -> existing lifecycle finally cleans artifacts, completes cancellation, releases permit
  -> shutdown waits for all active completion signals
```

The implementation does not delete or clean an active artifact session directly during shutdown. That would race the synchronous worker. It signals cancellation and waits for the existing request-owned cleanup path instead.

## 6. Deadline behavior

`Settings.request_deadline_seconds` defaults to 900 seconds and is configurable with `REQUEST_DEADLINE_SECONDS`. Values at or below zero are rejected by `Settings` and by direct `RequestLifecycle` construction.

Each request owns its own `asyncio` deadline task; no global timer or background worker is introduced. On expiration, the task invokes `CancellationManager.expire_deadline()`. The synchronous pipeline then observes the existing cooperative cancellation checkpoints and raises the existing `AnalysisCancelled` exception. Its ordinary lifecycle `finally` block performs cleanup and permit release before the request returns.

## 7. Files changed

| File | Change |
| --- | --- |
| `src/api/health.py` | New liveness, readiness, and combined health router. |
| `src/main.py` | Registers health router, passes deadline configuration into lifecycle, and uses FastAPI lifespan shutdown. |
| `src/api/request_lifecycle.py` | Adds request-local deadline registration, active cancellation tracking, graceful shutdown, and completion wait behavior. |
| `src/concurrency/admission.py` | Adds process-local admission closure for shutdown and its observable state. |
| `src/core/config.py` | Adds validated `request_deadline_seconds` configuration. |
| `.env.example` | Documents `REQUEST_DEADLINE_SECONDS=900`. |
| `tests/test_health.py` | Covers endpoint presence, successful live/ready/combined checks, and post-shutdown readiness failure. |
| `tests/api/test_request_lifecycle.py` | Covers deadline expiration, shutdown rejection, active cancellation, artifact cleanup, and permit release. |
| `tests/test_configuration_ownership.py` | Covers deadline environment configuration. |

## 8. Verification

| Command | Result |
| --- | --- |
| Focused health/lifecycle/configuration tests | 21 passed in 4.21s |
| `uv run pytest -q` | 214 passed in 4.98s |
| `uv run mypy src tests` | Success: no issues found in 139 source files |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check .` | Passed: 184 files already formatted |

## 9. Risks and limits

- Cancellation remains cooperative. A pipeline section that does not reach a cancellation checkpoint delays its own deadline response and graceful shutdown completion.
- Readiness deliberately becomes unavailable while the admission controller is at its configured capacity, because the process would immediately reject another analysis.
- The liveness and readiness state reports model initialization completed at application composition time; it does not run inference or perform a model reload probe.
- This phase does not alter upload ingress limits, worker counts, queues, model ownership, or analysis logic.

## 10. Next phase

**Phase 10.4 — Container verification and deployment closure.** Run the Compose deployment on a host with a Docker daemon, verify image build/start/health behavior with the mounted model volume, and close the remaining deployment verification blocker from Phase 10.2.

