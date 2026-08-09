# Phase 11.5 - Background analysis queue

## Objective and flow

Previously, `POST /analyze` resolved the video and waited for the full analysis. It now validates a
backend request, validates the relative video reference and callback destination, enqueues a small
job, and returns `202 Accepted` immediately. One lifespan-owned worker consumes jobs FIFO, runs the
existing analysis pipeline, and invokes the existing callback service.

```text
POST /analyze -> validate -> bounded FIFO queue -> 202
single worker -> existing analysis pipeline -> callback
```

## Contracts and job ownership

The request remains `videoId`, `playerId`, `videoUrl`, and `callbackUrl`. The response is:

```json
{"analysisId":"...","videoId":"video-123","playerId":"player-456","status":"queued"}
```

`AnalysisJob` contains only the analysis ID, backend IDs, safe relative video reference, callback
URL, and submission timestamp. It does not contain video bytes, paths, OpenCV handles, tensors, or
analysis output. The worker resolves the local path only when it begins execution.

## Queue and worker

`MAX_QUEUED_ANALYSES` configures waiting capacity (default 10). The queue is bounded in-memory FIFO
and has exactly one `AnalysisWorker` task, created during FastAPI lifespan startup. The existing
analysis lifecycle is entered only when a job becomes running, so its deadline begins at execution,
not while waiting. `MAX_RUNNING_ANALYSES` remains one; the queue adds waiting capacity only.

If full, the route returns HTTP 503 with `Analysis queue is full.` The rejected job is never queued,
run, retained, or callback-delivered.

## Callback and failure behavior

Successful and non-completed pipeline results use the existing callback payload builder and callback
retry policy. If pipeline execution fails after acceptance, the callback receives `status: failed`
with a sanitized error code and message. Callback delivery failure is logged separately and does not
change the analysis job from completed to failed.

## Cancellation, shutdown, and readiness

Queued jobs can be marked cancelled and are skipped without model execution. On shutdown the queue
stops admissions and cancels waiting jobs; the existing `RequestLifecycle.shutdown()` signals any
running analysis cooperatively, then the worker task is awaited/cancelled. Queued jobs are
non-durable and are lost/cancelled on process restart.

Readiness is true while a job is running when the queue still has capacity. It becomes unavailable
when the worker is absent, the queue is full/stopped, the lifecycle is shutting down, or storage/model
checks fail. Liveness semantics are unchanged.

## Tests, migration risk, and next phase

Tests cover 202 acknowledgement, bounded admission, FIFO execution, single concurrency, cancelled
jobs, worker survival after failures, queue-full HTTP behavior, callback behavior, and readiness
capacity. Existing analysis and Public Rating V2 tests remain unchanged.

The queue is intentionally process-local and non-durable. A restart can cancel accepted waiting jobs;
the next recommended phase is a deliberate durability/idempotency design before any multi-process or
multi-replica deployment.
