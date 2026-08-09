# Phase 9: Bounded Analysis Worker Architecture Design

## 1. Executive summary

The smallest safe next architecture is a **single-process bounded in-memory queue with
one existing analysis worker**. It accepts a bounded number of completed uploads,
executes one job at a time using the current application-scoped YOLO instances, and
preserves the current one-analysis model-safety boundary. It solves immediate rejection
for a bounded number of waiting users without changing model ownership, algorithms,
V2, or process topology.

It does **not** add parallel analysis. Multiple process workers are a later option,
not a present requirement: they require explicit IPC, global admission/result/file
ownership, and measured CPU/GPU/RAM budgets. A distributed broker system is not
justified by the current one-endpoint MVP evidence.

## 2. Repository constraints verified

| Constraint | Repository evidence |
|---|---|
| One Uvicorn process by default | Docker command omits `--workers`. |
| One active analysis | `DEFAULT_MAX_ACTIVE_ANALYSES = 1`; `main.create_app()` passes it to `AdmissionController`. |
| Immediate rejection/no queue | `AdmissionController.admit()` returns `None` when full. |
| Request-local tracking | `DetectionOnlyPlayerTracker.analyze()` creates tracker factory output and ball tracker per invocation. |
| Application-scoped YOLO | Both adapters and their `YOLO` models are constructed by `create_app()`. |
| Shared YOLO not concurrent-safe | Phase 8 records Ultralytics’ documented prohibition on shared model inference across threads. |
| Process-local operational state | `AdmissionController` and `ArtifactManager` use in-process maps/`threading.Lock`. |
| Synchronous pipeline offload | `AnalysisExecutor.execute()` uses `asyncio.to_thread()`. |
| Upload before admission | Route enters `temporary_upload()` before `execute_with_artifacts()`. |
| No capacity measurements | No CPU/RAM/GPU budget or executor-size configuration exists. |

## 3. Evaluated architectures

### Option A — bounded in-process queue, one current worker

The API owns a bounded FIFO queue and one worker consumes one job at a time using the
existing application-scoped models. This retains safe serial model use and request-local
trackers. It is the smallest change that changes overload behavior from immediate
rejection to bounded waiting. Its state is lost on process crash/restart, and it cannot
provide horizontal scaling by itself.

### Option B — multiple threads, shared YOLO protected by a lock

The lock would serialize the documented-unsafe shared model call, so the dominant YOLO
work remains one-at-a-time. Other parts could overlap, but the repository has no
resource measurement proving that overlap is useful or safe. It introduces lock
ownership/cancellation complexity without adding model throughput. It is not selected.

### Option C — a model instance for each concurrent thread

This follows the Ultralytics ownership rule but duplicates both models for every
concurrent thread. It requires an explicit bounded executor, model-initialization and
teardown ownership, CPU/GPU capacity measurements, and a decision on model affinity to
threads. The current application constructs one model pair, so this would be a material
model-ownership redesign. It is not selected before measurement.

### Option D — bounded worker processes

Each process initializes one player YOLO, one ball YOLO, and immutable analysis
services once; it runs one analysis at a time. This isolates models and failures and is
the correct future route to parallel analysis. It additionally requires IPC, a global
queue/admission authority, result delivery, crash handling, and worker/file ownership.
It is a later phase after a baseline and a one-worker queue characterization.

### Option E — external distributed worker system

An API/broker/worker system can offer durable cross-host queueing and recovery, but the
repository has no requirement for durable jobs, multi-host execution, or detached
results. It adds infrastructure and operational contracts beyond the present MVP. It is
not selected.

## 4. Decision matrix

| Criterion | A: one in-process worker | B: shared model + lock | C: model per thread | D: bounded processes | E: external workers |
|---|---|---|---|---|---|
| Correctness | excellent | good | good | excellent | excellent |
| YOLO safety | excellent | acceptable | good | excellent | excellent |
| ByteTrack isolation | excellent | excellent | excellent | excellent | excellent |
| Resource boundedness | good | acceptable | acceptable pending measurement | good pending measurement | good if configured |
| Memory cost | excellent | excellent | poor | acceptable | acceptable |
| CPU efficiency | acceptable | poor | unknown | good pending measurement | good pending measurement |
| GPU compatibility | excellent at one active run | acceptable | unknown | good pending measurement | good pending measurement |
| Failure isolation | poor | poor | poor | good | excellent |
| Cancellation | good, cooperative | acceptable | acceptable | acceptable; IPC required | good; durable protocol required |
| Operational complexity | excellent | acceptable | poor | acceptable | poor |
| Testability | excellent | acceptable | poor | good | poor |
| Current-product fit | excellent | poor | poor | acceptable | poor |
| Horizontal scalability | poor | poor | acceptable | good | excellent |

## 5. Recommended architecture

```text
current API process
  → bounded admission for persisted uploads
  → bounded FIFO queue
  → one analysis worker task
  → existing AnalysisExecutor / current application-scoped YOLO pair
  → one active analysis
  → Public Rating V2 response/result
```

The worker must own only scheduling of an existing analysis invocation; it must not
move or alter algorithms. The queue’s running limit remains one. The bounded queue
must be configured, not assigned an unmeasured production size.

This is the smallest safe option because it makes 2, 5, and 10 submitted jobs behave
as one running plus bounded queued jobs, instead of rejecting all but one, while never
calling the shared YOLO models concurrently.

## 6. Rejected alternatives

- **No queue/current behavior:** fails the stated waiting requirement.
- **Option B:** a model lock preserves serial inference but adds complexity without
  model throughput; current capacity already serializes safely.
- **Option C:** model duplication is unmeasured and changes ownership before a capacity
  decision exists.
- **Option D now:** process isolation is valuable, but its required global coordination
  is larger than the one-worker waiting problem.
- **Option E:** Redis, Celery, Kafka, a database, object storage, Kubernetes, and a
  tracing platform are not required by repository evidence for the MVP.

## 7. Queue contract

Configuration names, with no production values selected in this phase:

```text
MAX_RUNNING_ANALYSES = 1
MAX_QUEUED_ANALYSES
ANALYSIS_TIMEOUT_SECONDS
```

| Concern | Contract |
|---|---|
| Admission | Admit only after upload validation/persistence and only when `queued + running` is within configured capacity. |
| Ordering | FIFO among accepted, non-cancelled jobs. |
| Queue full | Reject explicitly; do not retain a job or upload beyond the configured bound. |
| Running bound | Exactly one worker starts one job at a time. |
| Queued cancellation | Remove job from queue, mark cancelled, release queued upload/artifact ownership. |
| Running cancellation | Record cooperative cancellation; release only after the current native stage reaches an existing checkpoint. |
| Timeout | Record expiry/cancellation intent; same cooperative running behavior. |
| Results | One job owns one internal result until it is delivered/retained under configured result policy. |
| Retry | No automatic retry by default: video analysis has local files and side effects; a retry policy requires an explicit idempotency/retention design. |
| Duplicate submission | Treat each accepted upload as a separate job unless a future client-supplied idempotency contract is introduced. |
| Restart/crash | In-memory queued/running metadata is lost. Durable recovery is not supported by Option A. |

## 8. Job lifecycle

Only necessary states are proposed:

```text
accepted → queued → running → completed
                       ├→ failed
                       ├→ cancelled
                       └→ expired
queued → cancelled
queued → expired
```

Minimal internal `AnalysisJob` fields:

```text
job_id, analysis_id, input_path, submitted_at, state,
cancellation_manager, attempt, artifact-session reference
```

`attempt` starts at one and does not imply retry. The job is not a generic workflow
engine; it is an ownership record for one uploaded video and one pipeline invocation.

## 9. Upload and file ownership

### Current behavior

The FastAPI handler receives multipart input and `temporary_upload()` writes the body
to a temporary file before calling lifecycle admission. Thus it cannot make an
application-level admission decision before the route has an `UploadFile` and enters
its upload persistence path. Repository code contains no server/proxy body-admission
mechanism, so pre-body rejection cannot be claimed from the current application.

### Recommended Option-A behavior

Choose **B: accept a validated upload, then queue it**, bounded by both queue capacity
and upload/artifact disk budget. The queue job owns its persisted input while queued;
the worker owns its read use while running; terminal cleanup removes it unless a
separate retention policy requires otherwise. Queue-full, queued cancellation, timeout,
and startup reconciliation must remove the queued input.

This preserves current multipart semantics. It means disk use can grow with accepted
queued uploads, therefore `MAX_QUEUED_ANALYSES` must be paired with an upload-byte and
temporary-disk budget. Current HTTP upload limits protect individual upload size, not
aggregate queued disk use.

For Option A, API crash/restart cannot reliably reconcile in-memory jobs with files;
startup may only perform an explicit local orphan cleanup policy. Durable recovery
requires durable job and file ownership outside the scope of this phase.

## 10. Process and model ownership

### One-worker design now

Initialized once in the API/worker process:

- `YOLOPlayerDetector` and player Ultralytics YOLO;
- `YOLOBallDetector` and ball Ultralytics YOLO;
- immutable settings and configuration-only analysis services.

Created per job:

- `ByteTrackTracker` and lazy `BYTETracker`;
- `NearestNeighborBallTracker`;
- validation/tracking/debug OpenCV handles;
- decoded frames, movement/interactions/event candidates/results;
- cancellation/job context and artifact session.

The model pair is not reloaded per video. Existing code already owns models at process
scope and trackers at analysis scope; Option A retains that safe serial arrangement.

### Process-worker design later

Each worker process must initialize the listed model pair and immutable services once,
then consume at most one job at a time. The global job allocator—not a worker-local
`AdmissionController`—must own queue capacity. It must provide workers job input and
receive terminal state/result. Artifact paths require globally unique job IDs and a
single documented file owner; process-local locks do not coordinate workers.

## 11. CPU design

For CPU-only operation, one worker remains the only characterized safe execution
configuration. A later process-worker trial must measure per-worker CPU use, native
thread count, decoder activity, YOLO time, and contention. It must not assume Python
threads provide CPU scaling: `asyncio.to_thread()` moves blocking work off the event
loop, but the repository has no executor or CPU-thread budget.

## 12. GPU design

The repository default is `model_device="cpu"`. For a future GPU deployment:

| Case | Design constraint |
|---|---|
| One GPU, one worker process | One model pair/context; safe serial inference baseline. |
| One GPU, multiple worker processes | Each process duplicates models and CUDA context; concurrent inference/memory fit must be measured before any count is selected. |
| Multiple GPUs | Worker-to-device assignment, one model pair per worker/device, and per-device admission must be explicit. |

Benchmark model VRAM residency, peak allocated VRAM, inference latency, utilization,
and CUDA-context overhead. No worker count can be selected from repository evidence.

## 13. Bounded resource formulas

Let `W` be model-worker processes, `R` running analyses, `Q` queued jobs, with
`R ≤ W` and `Q ≤ MAX_QUEUED_ANALYSES`.

```text
RAM_total ≈ API_runtime
          + W × M_worker_models
          + R × M_active_request(video)
          + Q × M_queued_metadata
          + runtime/thread overhead

temporary_disk ≤ Σ accepted queued/running upload sizes
               + Σ retained debug-source/artifact sizes

open_handles ≤ R × (validation + tracking + optional camera + optional debug handles)
```

`M_active_request` includes decoded frame buffers, per-frame player/ball maps,
trajectory/event/interactions data, and optional camera-motion grayscale-frame list.
The current renderer may create debug video and one image per frame when enabled.
Result-retention memory/disk must be bounded by an explicit terminal-result policy.

## 14. Failure model

| Failure | One-worker in-memory support | Requires durable/external state for recovery |
|---|---|---|
| API process crash/restart | Process stops; in-memory queue/jobs disappear; local files may remain | Yes, for job/result recovery |
| Worker crash in later process design | Mark only if allocator observes lost worker; job input may remain | Yes, for durable reassignment/result recovery |
| Model initialization failure | Fail worker/app startup; no analysis begins | No for one process; supervision policy needed later |
| Worker OOM | Process may terminate; active job is lost | Yes, for reliable retry/recovery |
| Queue full | Explicit local rejection and upload cleanup | No |
| Queued cancellation | Local removal/cleanup | No while process remains alive |
| Running cancellation/timeout | Existing cooperative checkpoint behavior | No, but immediate native-call termination is unsupported |
| Upload/artifact cleanup failure | Existing cleanup reports/errors and permit release behavior | Durable cleanup accounting requires external persistence |
| Malformed video | Existing validation failure path | No |
| Application shutdown | Stop accepting; cancel queued work and request cooperative cancellation for running work | Durable drain/restart semantics require external state |

## 15. Cancellation semantics

Queued cancellation is deterministic: remove the job before worker start and clean its
owned input/artifacts. Running cancellation and timeout use the existing
`CancellationManager`/checkpoint mechanism. They do not interrupt an active OpenCV or
YOLO call. The worker must not release its running slot, upload, or artifact session
until the synchronous invocation returns and its existing lifecycle cleanup completes.

## 16. Synchronous versus asynchronous API decision

Keep the current synchronous `POST /analyze` contract for the next bounded-one-worker
queue phase. It preserves Public Rating V2 and current frontend behavior, and a bounded
queue can make a limited number of users wait safely.

However, synchronous waiting includes upload time + queue wait + analysis time and is
subject to client/proxy timeout and disconnect behavior. The repository provides no
frontend or proxy timeout configuration from which a safe maximum wait can be chosen.
An asynchronous job API becomes appropriate only after measured queue/run durations or
product requirements demonstrate that synchronous waiting is unsuitable. It is not
implemented or selected as a current API change.

## 17. Benchmark requirements

Phase 9.1 must measure representative project clips in short, medium, and longer
durations, without assigning production thresholds in advance.

Per video measure: upload, validation, decoding, player YOLO, ball YOLO, ByteTrack,
post-processing, V2 mapping/serialization, and total response time.

Per process measure: RSS before models, after player model, after ball model, peak RSS
for one analysis, CPU use, thread count, temporary disk, debug-artifact disk, and open
file handles. When GPU is enabled measure model VRAM residency, peak allocated VRAM,
utilization, and inference latency. Repeat for the selected future worker/process
configuration before increasing any limit.

## 18. Staged implementation plan

1. **Phase 9.1 — baseline:** add observation-only timing/resource measurement around
   current one-analysis operation; no capacity change.
2. **Phase 9.2 — bounded one-worker queue:** implement queue/job lifecycle using the
   current model/process ownership; retain one running job.
3. **Phase 9.3 — worker abstraction:** isolate scheduler/job ownership from the API
   while preserving one local worker and V2 parity.
4. **Phase 9.4 — process-worker characterization:** build a deterministic isolated
   process-worker experiment with explicit IPC/file/result ownership; do not scale it.
5. **Phase 9.5 — measured multi-worker decision:** select worker/device capacity only
   from Phase 9.1/9.4 measurements and failure tests.
6. **Phase 9.6 — asynchronous API decision:** introduce a job API only if measured
   synchronous waiting or product requirements justify a public-contract change.

Every step must retain existing result mapping, request-local trackers, cleanup, and
admission/resource bounds.

## 19. Risks

- An in-memory queue improves waiting but has no crash/restart durability.
- Queueing after upload increases aggregate temporary-disk exposure.
- A synchronous client can time out while queued; current code has no durable result
  retrieval after disconnect.
- Multi-process scaling without a global allocator would recreate independent local
  capacity limits and cannot bound total work.
- GPU multi-process inference can duplicate model and CUDA-context memory; no count is
  safe without measurement.
- Cooperative cancellation latency remains governed by native call duration.

## 20. Explicit next phase

**Phase 9.1: performance and resource baseline.** Measure the current single-worker
pipeline and repository clips before selecting any queue size, worker count, timeout,
or future CPU/GPU concurrency configuration.
