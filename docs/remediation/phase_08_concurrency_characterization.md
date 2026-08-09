# Phase 8: Concurrency Characterization

## Executive summary

The deployed Docker command starts one Uvicorn worker process and this repository sets
`DEFAULT_MAX_ACTIVE_ANALYSES = 1`. `AdmissionController` is process-local and rejects
instead of queueing. Therefore, within one application process, at most one request can
execute the synchronous analysis pipeline at a time. For overlapping admission attempts,
2 requests produce at most 1 admitted/1 rejected, 4 produce at most 1/3, and 8 produce
at most 1/7. Upload persistence occurs before admission, so upload parsing and temporary
file creation can overlap.

The Phase 7.1 request-local ByteTrack migration removed the demonstrated mutable tracker
cross-request state. It did not make model inference concurrent-safe. `YOLOPlayerDetector`
and `YOLOBallDetector` each hold one application-scoped Ultralytics `YOLO` instance and
would be shared by concurrent worker threads if admission were increased. Ultralytics
explicitly documents shared `YOLO` instances across threads as unsafe due to races and
internal-state corruption; it requires dedicated model instances per thread or explicit
serialization. [Ultralytics thread-safe inference guide](https://docs.ultralytics.com/guides/yolo-thread-safe-inference/)

This phase changes no code. Classifications below mean:

- **SAFE**: an explicit repository lock/ownership rule or library guarantee covers the
  stated access pattern.
- **UNSAFE**: repository code or library documentation identifies a conflicting shared
  mutable access pattern.
- **UNKNOWN**: no applicable explicit guarantee was found. It is not a safe-to-share
  classification.

## Ownership and lifecycle matrix

| Component | Scope / lifetime | Mutable state | Cleanup guarantee | Evidence |
|---|---|---|---|---|
| `Settings` / immutable config | Application; `create_app()` lifetime | None after construction | Python lifetime | Frozen dataclasses in `core.config` |
| `YOLOPlayerDetector`, `YOLOBallDetector` | Application/process; constructed by `create_app()` | Wrapper references settings, logger, and one model | No explicit model teardown | `main.py`, adapters |
| Ultralytics `YOLO` | Application/process, one player and one ball instance | Third-party internal state not inspected | No repository teardown | Adapter constructors call `YOLO(path)` |
| `DetectionOnlyPlayerTracker` | Application/process | References detector/factories/settings only; no request tracker field | Python lifetime | `player_tracker.py` |
| `ByteTrackTracker` / lazy `BYTETracker` | Request analysis invocation | `_tracker`, `_seen`, counters | Becomes unreachable after `analyze()` returns/raises | Tracker factory is called at start of `analyze()` |
| `NearestNeighborBallTracker` | Request analysis invocation | last detection, missing count, history, segments, rejection count | Becomes unreachable after `analyze()` | Constructed inside `analyze()` |
| `cv2.VideoCapture` for validation/tracking/camera/debug | Invocation | Native decoder/handle state | Validator/tracker use `finally`; camera explicit post-loop release; debug renderer `finally` | respective service modules |
| `cv2.VideoWriter` for debug | Invocation, only when requested | Native encoder/handle state | Debug-renderer `finally` releases a created writer | `debug_renderer.py` Phase 7.2 |
| Analysis services (`TechnicalEventAnalyzer`, `BallInteractionAnalyzer`, movement, pass, shot, scoring, selection, proximity) | Application, holding settings/config only | Invocation-local lists/dicts/results | Python lifetime for services; invocation data becomes unreachable | constructors and `analyze`/`score` methods |
| `TechnicalScorer`, `CameraMotionEstimator`, event arbitrator | Invocation where constructed | Invocation-local calculations/results | Capture inside estimator released after read loop | `routes.py`, service modules |
| `FeatureExtractor` | Application | None | Python lifetime | no instance fields |
| `RequestLifecycle` / `AnalysisExecutor` | Application/process | References collaborators; executor has no mutable instance state | Lifecycle `finally` performs owned cleanup | `request_lifecycle.py`, `executor.py` |
| `AdmissionController` | Application/process | permit/counter totals protected by `threading.Lock` | Permit release is idempotent; lifecycle releases after admission | `admission.py` |
| `ArtifactManager` | Application/process | session and retained-directory maps protected by `Lock` | Session cleanup removes partial files and completes manager state | `artifacts.py` |
| `ArtifactSession` / `AdmissionPermit` / `CancellationManager` | Request | lifecycle, reservation, cancellation, and permit state | Phase 7.2 nested `finally`; session cleanup is idempotent | `artifacts.py`, `cancellation.py`, `request_lifecycle.py` |
| `asyncio.to_thread` work item | Invocation, on event loop default executor | ContextVar context and supplied callable state | Await returns result/exception; no repository executor shutdown | `executor.py` |
| NumPy arrays / decoded frames | Invocation | Mutable array buffer | Released when references disappear | decoder loop and local algorithm values |
| PyTorch tensors returned by Ultralytics | Invocation | Third-party tensor/storage state | Adapter converts results with `.cpu().tolist()` | YOLO adapters |

## Thread-safety matrix

| Component | Classification | Concurrent read | Concurrent write | Process safety | Evidence / required mitigation |
|---|---|---|---|---|---|
| Application-scoped YOLO adapters | **UNSAFE** for concurrent `detect()` | Not established because `.predict()` shares the model | Unsafe through shared model invocation | Separate processes own separate adapters/models | Underlying YOLO is shared; do not increase admission without dedicated model ownership or serialization. |
| Ultralytics `YOLO` | **UNSAFE** when shared across threads | Guide says do not share one instance | Unsafe; documented race/internal-state corruption | Per-process model instance; no inter-process sharing in repository | [Official guide](https://docs.ultralytics.com/guides/yolo-thread-safe-inference/) requires one model per thread or a lock. |
| `ByteTrackTracker` / `BYTETracker` | **SAFE under current ownership**; **UNSAFE if shared** | Not applicable to concurrent requests because request-local | Mutable `_seen`, counters, and third-party tracker state | Request-local object is not process-shared | Factory creates one per `analyze`; retain this ownership. |
| `DetectionOnlyPlayerTracker` | **SAFE under current admission only** | It shares detector wrappers | It does not mutate its own tracker state, but calls shared detectors | App-local | Increasing admission exposes the adapter/YOLO unsafe path. |
| `NearestNeighborBallTracker` | **SAFE under current ownership** | No cross-request sharing | Mutable state is request-local | Request-local | Constructed per `analyze()`. |
| OpenCV `VideoCapture` / `VideoWriter` | **UNKNOWN** as shared handles | No documented guarantee used here | No documented guarantee used here | Native handles are not process-shared by repository | Current ownership is one handle per invocation; do not share a handle across requests without a library guarantee. |
| `TechnicalEventAnalyzer` | **SAFE under repository access pattern** | Reads settings; invocation data is separate | Writes only local collections/results | App-local | No instance state is assigned during `analyze()`. |
| `BallInteractionAnalyzer` | **SAFE under repository access pattern** | Reads settings | Writes local collections/results | App-local | No instance state is assigned during `analyze()`. |
| `TechnicalScorer` / `FeatureExtractor` | **SAFE under repository access pattern** | Invocation inputs only | Local result construction | Invocation/app-local as composed | No mutable instance fields. |
| `RequestLifecycle` | **SAFE for its counters/resources under current code** | Collaborators use their own synchronization | Per-request state is local | Process-local | Lifecycle creates request objects; its shared collaborators are covered separately. |
| `AdmissionController` | **SAFE within one process** | Metrics are lock-protected | Admit/release are lock-protected | **UNSAFE for cross-process global capacity** | `threading.Lock` is process-local; no inter-process coordinator. |
| `ArtifactManager` | **SAFE within one process** | Manager maps lock-protected | Session creation/completion lock-protected | **UNKNOWN/unsupported across processes sharing the same root** | No cross-process filesystem lock or shared registry exists. |
| `CancellationManager` | **SAFE under current request ownership** | `Event`/state access guarded as implemented | transitions guarded by `Lock` | Request-local, not process-shared | `cancellation.py`. |
| `asyncio.to_thread` executor | **UNKNOWN as a capacity boundary** | Python documents offload/context propagation, not repository pool size | Multiple submissions may occupy default-executor threads | Per event loop/process | [Python docs](https://docs.python.org/3.12/library/asyncio-task.html#asyncio.to_thread); no executor size is configured. |
| NumPy `ndarray` | **SAFE for separate request-owned arrays; UNSAFE for shared mutation** | Read-only shared use is the documented preference | Concurrent mutation is race-prone; resize while read can crash | Normal arrays are not process-shared by repository | [NumPy thread safety](https://numpy.org/doc/2.3/reference/thread_safety.html). |
| PyTorch tensors | **UNKNOWN for shared threaded inference in this repository** | No repository-level tensor sharing occurs | No repository-level tensor mutation occurs | No tensors are sent between processes | Only `.cpu().tolist()` outputs are retained. PyTorch multiprocessing/CUDA constraints do not supply a shared-thread model guarantee. [PyTorch CUDA semantics](https://docs.pytorch.org/docs/stable/notes/cuda.html) |

## Dependency and concurrency graphs

```text
one Uvicorn process / one FastAPI app
  ├─ application-scoped: two YOLO adapters → two Ultralytics YOLO instances
  ├─ application-scoped: AdmissionController(max_active_analyses = 1)
  ├─ application-scoped: ArtifactManager / AnalysisExecutor / analysis services
  └─ each admitted request
       ├─ CancellationManager + ArtifactSession + temporary upload
       ├─ asyncio.to_thread synchronous pipeline
       │    ├─ request-local ByteTrackTracker → lazy BYTETracker
       │    ├─ request-local NearestNeighborBallTracker
       │    ├─ invocation-local VideoCapture, frames, NumPy arrays, tensors/results
       │    └─ optional invocation-local debug VideoCapture/VideoWriter
       └─ finally: artifacts → cancellation → permit → temporary upload context
```

```text
N simultaneous uploads (same process)
  upload staging may overlap
  │
  ├─ first request reaching admission: admitted → one worker-thread pipeline
  └─ each other request reaching admission before release: rejected immediately

No admission queue; no second admitted analysis while active_permits == 1.
```

## What happens at 1, 2, 4, and 8 simultaneous analyses

These results apply to overlapping admission attempts in one configured application
process. Arrival order is not defined by the repository.

| Attempts | Admitted analyses | Rejected analyses | Worker pipeline concurrency | Model concurrency |
|---:|---:|---:|---:|---:|
| 1 | 1 | 0 | 1 | 1 request invokes player then ball YOLO serially per frame |
| 2 | at most 1 | at least 1 while the permit is held | 1 | 1 |
| 4 | at most 1 | at least 3 while the permit is held | 1 | 1 |
| 8 | at most 1 | at least 7 while the permit is held | 1 | 1 |

If an earlier request releases before a later request calls `admit()`, that later
request can be admitted; there is no FIFO or waiting queue. Rejected routes do not
start tracking/model work, but their temporary uploads are still closed and removed.

## Failure modes and exact exposure

| Failure mode | Current state | Evidence |
|---|---|---|
| Shared ByteTrack corruption | Prevented for requests admitted by this app | Phase 7.1 factory creates one tracker per `analyze()`. |
| Shared YOLO model race/corruption | Prevented only because admission serializes analysis; would occur if concurrent calls share current instances | Ultralytics documented unsafe shared-thread model pattern. |
| Admission race / negative capacity | Prevented within process | `AdmissionController` lock and idempotent permit release; test coverage. |
| Cross-process admission race | Uncontrolled | Controller has no inter-process state. |
| Artifact directory collision | Prevented within process for one request ID | Manager lock and UUID request IDs; no cross-process lock. |
| Artifact disk pressure | Bounded per session only for reserved artifacts; debug frame-directory writes are not individually reserved | `ArtifactSession.reserve`; debug renderer writes frame files directly. |
| CPU starvation / thread exhaustion | Not observable at current max 1; unbounded default executor size is not configured by repository | `asyncio.to_thread()` uses default executor; no executor configuration. |
| GPU contention / out-of-memory | Not observable at default CPU setting and max 1; unknown if device changed | Settings default `model_device="cpu"`; repository has no GPU memory budget/telemetry. |
| Memory amplification | One active run retains per-frame tracking dictionaries; camera motion retains selected grayscale frames; debug retains trajectory/output | `player_tracker.py`, `camera_motion.py`, renderer. |
| File-handle leak in renderer | Addressed for Python exception paths in Phase 7.2 | Renderer finally releases created handles. |
| Cancellation latency | Present | Cancellation checks occur at stage boundaries; native decode/inference is not interrupted mid-call. |
| Deadlock | No repository deadlock path demonstrated at max 1 | Locks are short critical sections; third-party/model internals are not characterized. |

## Resource lifecycle

```text
temporary upload → admitted permit → cancellation + artifact session
  → worker-thread pipeline
    → invocation captures/frames/tensors
    → optional writer/debug files
  → ArtifactSession.cleanup (partial files/session directory according to retention)
  → CancellationManager.complete
  → AdmissionPermit.release
  → temporary-upload context closes and unlinks upload
```

The Phase 7.2 lifecycle ensures permit release after admitted setup, executor, worker,
artifact-cleanup, and cancellation-completion paths. Debug capture/writer release is
attempted once after a successful creation, including rendering exceptions.

## Capacity model

No measured CPU, RAM, GPU-memory, OpenCV, or disk figures exist in the repository, so
numeric capacity cannot be determined. Let:

- `Mmodels` = resident memory of the application’s two YOLO objects;
- `Mrequest(video)` = decoded-frame/transient tracking/response memory for one active
  analysis;
- `Mcamera(segment)` = retained grayscale frames when debug-source camera motion runs;
- `Dupload` = temporary upload bytes;
- `Ddebug` = copied source plus optional debug video/frame output bytes.

At the current limit, application memory is approximately:

```text
Mprocess ≈ Mmodels + Mrequest(video) + optional Mcamera(segment) + runtime overhead
```

Staged disk can include one active `Dupload` plus its request artifacts, while uploads
that have not reached/reached rejected admission can temporarily consume their own
`Dupload`. No queue pressure exists because the controller rejects rather than stores
pending analyses.

If application admission were changed to `C`, the minimum request-state term would be
approximately `C × Mrequest`; camera/debug terms scale with enabled requests. Current
model objects are application-scoped, so there is no per-request model duplication in
the existing code. A safe per-thread-YOLO design required by Ultralytics would instead
require at least `C × Mmodels` model residency (or a lock, which serializes inference).
Exact values require measurement on the deployment hardware and configuration.

## Blocking constraints for safe parallel execution

1. The two application-scoped Ultralytics YOLO instances cannot be called
   concurrently from worker threads under the documented library guidance.
2. No selected model-ownership strategy exists for concurrent inference: dedicated
   model instances, a serializing lock, or another explicitly characterized execution
   boundary.
3. No configured executor size, CPU budget, GPU memory budget, or file-handle/disk
   capacity exists for more than one active analysis.
4. Admission and artifact coordination are process-local; multiple workers/processes
   would independently admit work and duplicate models.
5. OpenCV handle sharing has no documented safety guarantee; the current per-invocation
   ownership must remain intact.
6. Cooperative cancellation does not preempt decoder/model native calls, so capacity
   is retained until the next stage boundary.

## Exact prerequisites for safe concurrency

Before increasing `max_active_analyses`, all of the following must be decided and
verified with deterministic tests and deployment measurements:

1. Choose a documented-safe YOLO ownership model for every concurrent execution;
   do not share the present model instances concurrently.
2. Keep `ByteTrackTracker`, `BYTETracker`, ball trackers, captures, writers, arrays,
   cancellation state, and artifacts request-local as they are now.
3. Define and configure a bounded execution capacity consistent with the chosen model
   ownership—not merely a larger admission integer.
4. Establish measured CPU, RAM, GPU, temporary-disk, debug-artifact, and file-handle
   budgets per active analysis.
5. Define multi-process behavior, because existing admission/artifact coordination is
   not global across Uvicorn workers.
6. Add concurrent inference, cleanup, cancellation-latency, artifact-root, and
   resource-exhaustion characterization tests using the selected ownership model.

## Recommended phase order

1. Characterize a single selected safe YOLO-concurrency ownership strategy with
   isolated test models and explicit resource measurements.
2. Characterize bounded executor and cancellation behavior for that strategy.
3. Characterize multi-process admission/artifact/model ownership only if multiple
   workers are required.
4. Only then propose an admission-capacity change. Do not increase it in this phase.
