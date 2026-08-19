# ADR-006: Controlled concurrency MVP

## Status

Accepted for the controlled MVP. This decision supersedes neither ADR-004 nor ADR-005: those remain the approved future durable-job design.

## Immediate objective

The immediate objective is controlled concurrency and measured capacity on the 4-vCPU, approximately 15-GiB VPS, not durable distributed job execution. Super-7 will retain its process-local queue while evidence is gathered.

## Selected MVP architecture

- One FastAPI API process retains the existing bounded in-memory admission queue.
- At most two CPU-heavy video analyses will run in separate long-lived processes, one analysis at a time in each process. The configured default remains one until the VPS benchmark approves two.
- Video analysis must not run in the FastAPI event loop. A `ThreadPoolExecutor` is not an acceptable final boundary for this CPU-heavy pipeline.
- Lightweight network I/O, including callback HTTP delivery, may remain asynchronous.
- No Redis, Celery, Kubernetes, PostgreSQL runtime integration, autoscaling, multiple hosts, new database server, or direct Apex-table writes are part of this MVP.

The current code uses `AnalysisExecutor.execute()` with `asyncio.to_thread`, but that is a current implementation fact, not the selected future CPU boundary. The recommended mechanism is a small, supervised set of long-lived `multiprocessing` worker processes, with simple job/result messages and one model/pipeline composition per worker. This is preferred over `ProcessPoolExecutor` because the current request pipeline is a closure over many service instances and lifecycle objects, and workers need explicit startup, health, cleanup, and ownership. It is preferred over passing work by `fork` because worker construction can be made explicit. The exact message and supervisor design is deferred to MVP-2 after process-safety tests.

## Explicit non-durability

For this MVP, HTTP `202 Accepted` means only that the current process placed the request in memory; it is not durable acceptance. Queued and running jobs, in-memory states/results, and callback retries can be lost on process, container, or host restart. Super-7 cannot guarantee recovery, exactly-once execution, or retention of queued work after restart. Duplicate execution can occur around retries or restarts. These limitations must be disclosed to Apex and must not be presented as production durability.

## Apex responsibilities and unresolved decisions

During the MVP, Apex should persist its own logical request/job record; send a stable request/job correlation identity within an agreed future contract; deduplicate repeated terminal callbacks as successful no-ops; identify requests unresolved after an agreed timeout; and provide controlled manual or policy-based retry. Apex must not assume Super-7 retains queued work after restart. Super-7 receives no direct Apex database access.

Unresolved cross-team decisions are the correlation identity location/format, the unresolved-job timeout, retry authority and policy, and the callback deduplication key. The proposed durable V1 idempotency and callback-event identifiers in ADR-004 and the contract remain future work, not current API behavior.

## Admission and backpressure policy

The current route creates an `AnalysisJob`, submits it to `AnalysisQueue`, and returns `503 Service Unavailable` when the queue rejects it. Its default queue capacity is 10 (`MAX_QUEUED_ANALYSES`), while current active analysis capacity is separately one (`DEFAULT_MAX_ACTIVE_ANALYSES`). Queued requests and running analyses are therefore counted separately.

The implementation phase should preserve a bounded queue, set active process capacity independently to one then conditionally two, and return `503` before accepting a job when queue capacity is exhausted or the service is shutting down. `503` fits temporary service capacity rather than a client-specific rate limit; `429` would require an agreed per-client rate-limit policy. There must be no unbounded in-memory accumulation and no claim of a durable job on rejection.

## Shutdown limitations and policy

Today the FastAPI lifespan stops queue admission, drains waiting queue entries by marking them `CANCELLED`, asks `RequestLifecycle` to cooperatively cancel active work and await completion, then cancels the worker task. The pipeline currently runs through `asyncio.to_thread`; cancelling its awaiting task cannot forcibly stop a running native/threaded pipeline. A process/container/host stop can therefore lose queued work, active work, state, results, and undelivered callback retries.

The implementation phase should first stop admissions, report queue and active counts, allow a bounded configurable grace period for active process workers and lightweight callbacks, then terminate remaining child processes and log each unresolved correlation ID. Graceful shutdown reduces avoidable loss but cannot provide recovery without the deferred durable design.

## Process and model-loading analysis

`main.create_app()` constructs `YOLOPlayerDetector` and `YOLOBallDetector` as components of the lifespan-owned tracker. The adapters defer `ultralytics.YOLO(...)` creation until their first `detect()` call and cache the resulting model on the adapter. `DetectionOnlyPlayerTracker` also owns OpenCV frame decoding, ByteTrack construction per analysis, and the detector objects. The application currently shares those tracker objects with its one queue worker.

The existing pipeline is not demonstrated pickle-safe: its callable is a closure over tracker, services, logger, lifecycle, artifact session, and cancellation objects; loaded Ultralytics/PyTorch models and OpenCV-related objects must not be assumed serializable. Linux `fork` after threads, model initialization, or native-library initialization is unsafe and platform-specific; no fork-based sharing assumption is approved. Each future analysis process should start clean, construct its own settings, detector adapters, tracker, and pipeline, and receive only serializable job references. This duplicates model/runtime memory across workers; the amount, initialization time, CPU contention, and any model-sharing behavior require VPS measurement rather than estimation.

OpenCV, PyTorch, Ultralytics, FFmpeg, and numerical libraries may create internal threads, so two processes can oversubscribe four vCPUs. MVP-1/6 must record their observed thread and CPU behavior. Environment or library thread caps (for example OpenMP, BLAS, Torch, or OpenCV controls) are candidates only after measuring their effect; no specific cap is asserted here.

## Callback behavior

After analysis completes, `create_analysis_job_processor()` awaits `CallbackService.send_result()` inline before returning the terminal worker state; this applies to successful and failed analysis payloads. `CallbackService` performs transport in `asyncio.to_thread`, makes four total attempts, and waits 1, 2, then 4 seconds between retries for transport errors, timeouts, or non-2xx responses. Delivery failure is logged and does not change a completed analysis to failed. All retry state is in memory and is lost on restart. Because the processor awaits it, callback retry time currently occupies the queue worker's analysis slot.

The smallest MVP adjustment is to return terminal analysis output to the API-process supervisor and execute callback delivery in a separately bounded lightweight asynchronous path, retaining the same non-durable retry semantics. This must be designed and tested in MVP-4; it does not imply a durable outbox or callback recovery.

## Benchmark and acceptance plan

Before enabling two processes, capture a concurrency-one baseline and then compare a controlled concurrency-two trial. Measure API acceptance latency, queue wait time, end-to-end duration, per-job analysis duration, throughput, per-process CPU, host CPU load, peak and steady RAM, swap/OOM behavior, model initialization time, callback latency/failures, API responsiveness during analysis, and failure/temporary-resource cleanup.

Run one request, two simultaneous requests, four requests, queue saturation, invalid video, analysis exception, callback timeout/failure, shutdown while queued, and shutdown while running. The provisional go/no-go rules are: two is never the default before measurement; no OOM or container restart; API remains responsive; no third simultaneous analysis; temporary resources are cleaned; each no-restart test job has a visible terminal log outcome; and concurrency two is rejected in favor of one if it reduces throughput or creates unacceptable latency/resource pressure. Product and operations must agree numeric latency/SLA thresholds before rollout; none are invented here.

## Future durability trigger

ADR-004 and ADR-005 become implementation priority when the pilot expands to uncontrolled users, accepted-job loss becomes unacceptable, restart recovery is required, duplicate analysis becomes materially costly, callback delivery must survive restart, multiple API instances or hosts are introduced, deployment commonly occurs while work is queued, or operational evidence justifies durable persistence.
