# Phase 1.1 — Concurrency Isolation Design

**Design-only document.** This document proposes an incremental architecture for
isolating synchronous video analysis from the FastAPI event loop without adding a
durable queue or changing any analysis algorithm, threshold, score, public response,
or existing endpoint contract.

## Problem description

`POST /analyze` currently owns the complete analysis lifecycle. It accepts a multipart
upload, persists it temporarily, validates OpenCV metadata, runs player/ball model
work and tracking, performs selection and all analysis stages, copies a debug source,
renders artifacts, builds diagnostics, and returns the final response.

The endpoint is declared `async`, but the material work is synchronous: OpenCV calls,
YOLO loading/inference, tracking, camera-motion estimation, disk copying, debug video
encoding, JPEG writing, and response preparation all execute in the request path.
The application-level upload byte limit protects the persistence loop, but it does not
bound work concurrency, artifact accumulation, or the multipart body before parsing.

This creates event-loop starvation, head-of-line blocking, unpredictable latency,
unbounded concurrent resource demand, and weak cancellation/cleanup behavior. These
are operational architectural problems, independent of the correctness of detection,
tracking, events, or scoring.

## Root-cause analysis

### Symptoms

- One expensive request can delay unrelated requests.
- Concurrent analyses compete unpredictably for CPU, GPU, OpenCV handles, temporary
  storage, and debug-artifact disk space.
- A disconnect or timeout does not give the pipeline a coherent cancellation and
  cleanup boundary.
- Debug rendering increases the latency and resource footprint of the primary request.
- No component owns admission, execution capacity, cancellation, or artifact retention
  as first-class operational concerns.

### Architectural root causes

1. **HTTP orchestration and blocking execution are coupled.** The route owns both
   protocol handling and the full synchronous pipeline.
2. **No bounded execution domain exists.** The service graph is constructed once but
   no explicit capacity governs how many analyses may use it concurrently.
3. **Resource ownership crosses stages without a lifecycle authority.** Temporary
   upload files, OpenCV resources, source copies, debug frames, and final artifacts
   have different creators and incomplete exception-path ownership.
4. **Admission is validation-only, not capacity-aware.** A valid upload is allowed to
   begin costly work regardless of currently active analyses or artifact capacity.
5. **The response contract assumes synchronous completion.** This phase must preserve
   that contract, so isolation must occur beneath the current route rather than by
   changing the API into a job-submission API.

## Architectural goals

- Keep `POST /analyze` and every current response structure behaviorally compatible.
- Bound the number of simultaneous analysis executions and their associated resource
  ownership.
- Keep event-loop work limited to HTTP/multipart coordination and result handoff.
- Preserve the current pipeline ordering and all algorithm inputs, formulas, and
  thresholds.
- Define deterministic ownership and cleanup for uploads, OpenCV resources, and debug
  artifacts on success, failure, cancellation, and timeout.
- Make admission, rejection, cancellation, and cleanup observable with request IDs.
- Retain reproducibility metadata while preventing artifacts from becoming an unbounded
  side effect.
- Allow fakes in deterministic tests; do not require model inference or model download.
- Establish a design that can later evolve to durable jobs only if service-level
  requirements demand it.

## Proposed components

### AdmissionController

**Responsibilities**

- Decide whether a validated request may enter active analysis based on configured
  local capacity and resource budget availability.
- Allocate an analysis/request identifier before expensive work begins.
- Return a stable, request-correlated rejection when capacity is exhausted.
- Record admission, rejection, queue-wait-within-process, and completion metrics.

**Ownership boundary**

Owns capacity permits only. It never owns uploaded content, detectors, algorithms, or
artifacts.

**Lifecycle**

Created by the composition root and lives for the process lifetime. A permit is held
from successful admission through executor completion/cancellation and is released
exactly once.

**Dependencies**

- immutable operational settings for maximum active analyses and optional bounded wait;
- structured logger/metrics sink;
- `ResourceBudget` reservation decision.

**Invariants**

- active permits never exceed configured capacity;
- every accepted permit is released exactly once;
- rejected requests never begin model/OpenCV execution;
- admission does not mutate request-specific analysis data.

### AnalysisExecutor

**Responsibilities**

- Run the existing synchronous pipeline as one isolated execution unit outside the
  event-loop request thread.
- Preserve stage order, constructor-injected dependencies, inputs, outputs, and
  response mapping.
- Establish a single execution boundary for timeout/cancellation observation and
  exception translation back to the route.

**Ownership boundary**

Owns the execution context only. The existing pipeline services retain responsibility
for their algorithms; the executor must not become a replacement event engine or
service locator.

**Lifecycle**

Created by the composition root. Each admitted request receives one execution session
whose lifetime ends when its pipeline result, controlled failure, or cancellation is
reported. The underlying synchronous task must be allowed to reach a safe cleanup point
before its resources are considered released.

**Dependencies**

- explicitly injected existing pipeline collaborators;
- `CancellationManager` cancellation token/state;
- `ResourceBudget` reservation;
- `ArtifactManager` request-scoped artifact session;
- structured logging with request ID.

**Invariants**

- no algorithm formula, threshold, candidate acceptance outcome, or response shape is
  altered by the execution boundary;
- blocking pipeline code does not execute directly on the event-loop thread;
- an executor session cannot outlive its cleanup ownership;
- exceptions retain a request ID and are translated only at the existing HTTP boundary.

### ResourceBudget

**Responsibilities**

- Represent process-local limits for concurrent analysis, temporary-storage allocation,
  debug-artifact generation, and optional execution duration.
- Reserve and release declared resource capacity for one admitted analysis.
- Expose measurable usage and reservation failures.

**Ownership boundary**

Owns accounting, not the actual model, OpenCV object, or file. Individual resource
owners remain responsible for closing/removing their resource before a reservation is
released.

**Lifecycle**

Created once by the composition root. Each reservation is request-scoped and is closed
by the executor's lifecycle authority.

**Dependencies**

- immutable settings;
- clock and metrics/logger abstractions already available through standard runtime
  facilities.

**Invariants**

- reservations are bounded and released exactly once;
- reserved capacity cannot be exceeded by races;
- debug artifact capacity is evaluated separately from core analysis capacity so
  diagnostics cannot starve primary analysis;
- budget denial is explicit and observable, never a silent stall.

### CancellationManager

**Responsibilities**

- Maintain request-scoped cancellation state for client disconnect, configured request
  deadline, and service shutdown.
- Provide cooperative cancellation checkpoints only at safe stage boundaries.
- Coordinate cleanup ordering without attempting unsafe interruption of native OpenCV
  or model calls.

**Ownership boundary**

Owns cancellation intent and final state. It does not force-kill threads, mutate
algorithm data, or close resources owned by another component.

**Lifecycle**

Created per admitted request. It starts before executor handoff, observes disconnect,
deadline, and shutdown signals, and is finalized after executor cleanup/release.

**Dependencies**

- request lifecycle signal from the route;
- configured deadline;
- executor stage-boundary notifications;
- structured logger/metrics sink.

**Invariants**

- cancellation is idempotent;
- cancellation never changes an already-computed score or candidate result;
- resource cleanup runs whether cancellation occurs before, during, or after a safe
  stage boundary;
- an uninterruptible native call is tracked as still active until it returns.

### ArtifactManager

**Responsibilities**

- Own request-scoped debug-source copies, debug video, debug frames, and artifact
  manifests.
- Create artifacts only after budget/admission allows them and place them under a
  generated analysis directory.
- Enforce retention, size, and cleanup policy; expose opaque artifact references rather
  than filesystem paths when the public contract can be versioned in a later phase.
- Make partial artifacts distinguishable and removable after write failure/cancellation.

**Ownership boundary**

Owns only artifacts, never the original temporary upload or analysis result. It must not
rewrite source video and must not own detector/tracker state.

**Lifecycle**

One artifact session per admitted request. It is opened after validation, finalized on
successful rendering, and cleaned on controlled failure/cancellation according to a
retention policy. Process-level retention collection is a separate scheduled operation,
not route code.

**Dependencies**

- `ResourceBudget` artifact reservation;
- generated analysis ID;
- configured artifact root and retention policy;
- existing debug renderer as an injected producer.

**Invariants**

- no untrusted filename determines an artifact path;
- no partial output is represented as completed;
- artifact creation cannot consume unbounded disk;
- cleanup does not mask the primary analysis failure.

## Request lifecycle

```text
request received
  → multipart and video validation
  → request ID allocation and capacity admission
  → request-scoped resource, cancellation, and artifact sessions created
  → existing pipeline executed in the bounded analysis executor
  → optional artifact finalization within its separate budget
  → existing response mapping and diagnostics validation
  → executor/resource/artifact cleanup and permit release
  → response
```

Detailed lifecycle rules:

1. The route remains the HTTP boundary and continues to validate the current multipart
   contract.
2. Validation completes before an analysis permit is acquired only when validation work
   remains lightweight and bounded; otherwise upload staging itself obtains a distinct
   small admission reservation.
3. `AdmissionController` either grants a local permit or produces an explicit,
   request-correlated capacity result. It must not start model work while waiting
   indefinitely.
4. `AnalysisExecutor` receives immutable request input plus existing injected pipeline
   dependencies and runs the current pipeline order unchanged away from the event loop.
5. At defined safe stage boundaries, the executor observes cancellation/deadline state.
   Native calls already in progress are allowed to finish their safe cleanup sequence.
6. Artifact generation is managed as a separately budgeted optional phase. A debug
   failure remains non-fatal only where it is non-fatal today, but is logged and cleaned
   deterministically.
7. The executor returns the existing completed/ambiguous/non-completed response
   semantics to the route. The route performs only HTTP-safe exception mapping.
8. `finally` ownership closes OpenCV resources, temporary inputs, artifact sessions,
   reservations, and the admission permit in a defined order.

## Proposed folder structure

This is a proposed incremental layout, not a requirement to move existing algorithm
modules in one change.

```text
src/
├── api/
│   ├── routes.py
│   └── request_lifecycle.py          # HTTP-to-execution orchestration only
├── core/
│   ├── config.py
│   ├── exceptions.py
│   └── pipeline.py
├── concurrency/
│   ├── admission.py                  # AdmissionController
│   ├── executor.py                   # AnalysisExecutor
│   ├── budget.py                     # ResourceBudget
│   └── cancellation.py               # CancellationManager
├── diagnostics/
│   ├── artifacts.py                  # ArtifactManager and artifact lifecycle
│   └── metrics.py                    # operational event definitions
├── services/                         # existing detection and analysis modules remain
└── tests/
    ├── concurrency/
    ├── api/
    └── diagnostics/
```

`concurrency` contains only the five operational concerns in this design. Algorithms,
scoring, schemas, and existing services remain in place. `diagnostics` is justified by
the separate artifact lifecycle and operational metrics consumers; it does not absorb
domain analysis diagnostics.

## Required modifications

The following is an implementation inventory, not authorization to make these changes
in this design phase.

| File or area | Why it changes | Scope | Risk |
|---|---|---|---|
| `src/main.py` | Construct and inject operational components at the composition root. | Add explicit lifecycle ownership; preserve existing service construction. | Medium: startup and test wiring. |
| `src/api/routes.py` | Reduce route responsibility to HTTP validation, admission, executor handoff, and existing response mapping. | Preserve endpoint and response contract. | High: broad orchestration surface. |
| `src/api/request_lifecycle.py` | Isolate request lifecycle coordination from response-mapping helpers. | New thin orchestrator with no algorithms. | Medium: cancellation/error boundary. |
| `src/concurrency/admission.py` | Introduce bounded local admission. | New component, no queue persistence. | Medium: capacity behavior. |
| `src/concurrency/executor.py` | Isolate existing blocking pipeline execution. | New execution boundary using existing collaborators. | High: thread/process safety and cleanup. |
| `src/concurrency/budget.py` | Centralize process-local resource reservations. | New operational accounting. | Medium: incorrect accounting can reject work. |
| `src/concurrency/cancellation.py` | Define safe cancellation/deadline ownership. | New request-scoped state only. | High: native-call limitations. |
| `src/diagnostics/artifacts.py` | Own artifact retention and partial-output cleanup. | New lifecycle manager around current renderer. | High: user media retention and disk safety. |
| `src/services/video_validator.py` | Align temporary-file ownership with executor lifecycle. | Cleanup/error observability only; preserve validation rules. | Medium: Windows cleanup semantics. |
| `src/services/debug_renderer.py` | Make writer/capture cleanup exception-safe and report finalization to ArtifactManager. | Resource ownership only; preserve rendered content. | Medium: artifact behavior. |
| `src/core/config.py` and profile/config documentation | Add operational limits and validation for capacity, deadlines, artifact quota/retention, and debug enablement. | No analysis threshold change. | Medium: deployment defaults. |
| `src/core/exceptions.py` and API error mapping | Add stable capacity/deadline/cancellation error representation. | Backward-compatible extension or versioned policy only. | High: public HTTP semantics. |
| `tests/concurrency/`, `tests/api/`, `tests/diagnostics/` | Characterize behavior before implementation. | New deterministic tests using fakes. | Low: test maintenance. |
| deployment configuration, Docker, CI | Supply process limits, writable ownership, timeout coordination, and clean-checkout verification. | Infrastructure only. | Medium: environment-specific behavior. |

## Characterization tests

Write these tests before implementation, with fake tracker/analyzer/rendering
collaborators and no model inference:

1. **Concurrent admission:** start more requests than configured capacity; prove that
   only the allowed number begin execution, excess requests receive the chosen stable
   capacity outcome, and all permits are released.
2. **Event-loop responsiveness:** hold a fake blocking analysis in the executor while
   verifying an unrelated lightweight ASGI request is still served within a bounded
   interval.
3. **Request isolation:** run multiple accepted requests with distinct fake state and
   assert no observations, analysis IDs, artifacts, or cancellation state cross
   requests.
4. **Timeout before execution:** expire a request while it waits for admission; assert
   no detector/tracker call and no artifact directory.
5. **Timeout at safe boundary:** have a fake stage report a boundary after deadline;
   assert cleanup, permit release, and a stable request-correlated outcome.
6. **Client-disconnect cancellation:** simulate disconnect before and during analysis;
   assert cancellation intent is recorded and no resources are prematurely released
   while a fake native stage is active.
7. **Temporary upload cleanup:** exercise success, validation error, pipeline error,
   cancellation, and simulated unlink failure; assert primary error precedence and
   cleanup telemetry.
8. **OpenCV artifact cleanup:** inject capture/writer failures at open, write, and
   finalization; assert release calls, no completed partial artifact, and no absolute
   path in client-facing output.
9. **Artifact quota and retention:** exhaust a fake artifact budget; assert analysis
   behavior follows the selected existing-compatible debug-degradation policy and
   primary analysis capacity remains available.
10. **Parity regression:** run existing fake pipeline inputs through old and isolated
    paths; assert identical selected player, candidates, scores, warnings, diagnostics,
    versions, and response serialization.
11. **Startup and shutdown:** verify composition-root ownership starts once, refuses new
    work during shutdown, drains/records active work according to policy, and releases
    operational resources.

## Rejected alternatives

### Celery

Rejected for this phase because it adds broker, worker, serialization, deployment,
result-retention, and operational failure modes while the required public contract is
synchronous completion. It is appropriate only if durable asynchronous jobs become a
confirmed product requirement.

### RabbitMQ

Rejected because a broker alone does not define job lifecycle, result retrieval,
idempotency, or artifact ownership. It increases infrastructure complexity without
solving the immediate in-process admission and execution-isolation problem.

### Kafka

Rejected because event-stream infrastructure is disproportionate for one bounded,
request/response analysis operation. Ordering, retention, consumer groups, and schema
operations would be unnecessary complexity in this phase.

### Redis Queue

Rejected because it introduces an external queue and worker lifecycle while preserving
neither the current synchronous endpoint contract nor simple local resource ownership.

### Complete rewrite

Rejected because the existing analytical pipeline, typed contracts, and deterministic
tests provide useful stable seams. Replacing the system would increase regression risk
and delay mitigation of the narrow concurrency problem.

## Risks

### Implementation risks

- Offloading work can reveal non-thread-safe model/tracker or OpenCV assumptions.
- Unsafe cancellation of native calls can corrupt resource ownership; cancellation must
  stay cooperative and stage-boundary based.
- Changing error/status behavior can break API consumers if not introduced with an
  explicit compatibility plan.
- A broad executor can become a service locator or duplicate route orchestration if
  responsibilities are not kept narrow.

### Operational risks

- A local bounded executor protects one process but does not provide cross-process or
  cross-node coordination.
- Model memory, GPU allocation, temp storage, and artifact disk limits must be measured
  per deployment target before choosing defaults.
- Incorrect admission limits can produce avoidable rejection; permissive limits can
  still exhaust a host.
- Artifact retention and client access controls involve privacy and storage governance.

### Maintenance risks

- Operational settings add deployment complexity and must have documented defaults.
- Tests that assert concurrency behavior can become timing-sensitive unless fakes use
  deterministic synchronization points.
- Two execution paths during migration can drift unless parity tests are maintained.

## Migration strategy

1. **Characterize first.** Add the deterministic tests above around the current route
   with injected fakes. Capture response parity fixtures before moving work.
2. **Add operational objects without routing work through them.** Construct immutable
   budget/admission/cancellation/artifact dependencies at the composition root and
   verify they have no effect while disabled.
3. **Introduce bounded admission behind a disabled-by-default operational setting.**
   Enable it only in a non-production environment after rejection/metric semantics are
   agreed. Preserve current default behavior until deployment configuration is ready.
4. **Move the existing synchronous pipeline as one unit behind `AnalysisExecutor`.**
   Do not split or refactor algorithms. Compare old and new paths using parity tests.
5. **Make artifact ownership explicit.** Add deterministic cleanup/finalization and
   retention behavior while retaining the present artifact response contract during the
   compatibility period.
6. **Enable cancellation/deadline observation at safe boundaries.** Measure native
   stage duration before enforcing strict deadlines.
7. **Deploy one bounded process first.** Monitor admitted/rejected work, latency,
   resource usage, cleanup failures, and artifact growth. Increase capacity only from
   measured host budgets.
8. **Remove the temporary direct path only after parity, load, and cleanup criteria
   pass.** No queue is introduced unless requirements later demand durable detached
   jobs.

## Verification strategy

Success is measured with reproducible, environment-specific baselines:

- **Latency:** capture p50/p95/p99 upload-to-response latency and executor wait time
  by video size/duration; verify unrelated lightweight endpoint latency remains bounded
  under active analysis.
- **Concurrency behavior:** prove active execution never exceeds configured capacity;
  record capacity rejections and permit leaks; test controlled shutdown/drain behavior.
- **Memory and GPU:** record per-analysis peak RSS, model memory/GPU allocation, and
  aggregate usage at each supported capacity. Set budget defaults below host headroom.
- **Disk and file handles:** record temporary/artifact bytes, active OpenCV resources,
  cleanup failures, orphan count, and retention deletions. Require zero leaks in
  deterministic failure/cancellation tests.
- **Regression:** require old/new response-parity tests, existing unit/integration
  tests, Ruff, formatting, mypy, and full pytest to pass without model inference.
- **Clean deployment:** build and start the target image with declared model delivery,
  non-root writable directories, configured timeouts/body limits, and health checks.

## Final decision

### ADR-001

**Title:** Concurrency isolation without introducing a queue

**Status:** Accepted

**Context:** The single FastAPI endpoint performs blocking video/model/OpenCV/artifact
work directly in an async request path. It lacks bounded admission, execution isolation,
request-scoped cancellation ownership, and artifact lifecycle control. The current API
must remain synchronous and backward compatible in this phase.

**Decision:** Introduce process-local `AdmissionController`, `AnalysisExecutor`,
`ResourceBudget`, `CancellationManager`, and `ArtifactManager` through the existing
composition root. Keep the existing pipeline as one behavior-preserving execution unit
outside the event-loop request thread. Use bounded local capacity, cooperative safe
boundary cancellation, explicit artifact ownership, and deterministic cleanup. Do not
introduce Celery, RabbitMQ, Kafka, Redis Queue, or a system rewrite.

**Consequences:** The service gains explicit capacity, lifecycle, and observability
boundaries while retaining synchronous request/response semantics. Deployment must
provide measured resource limits and startup/shutdown policy. A future durable queue
remains possible but is not required for this phase.

**Trade-offs:** Local isolation does not provide durable jobs, cross-node scheduling,
or immediate interruption of native model/OpenCV calls. It adds a small set of
operational components and characterization tests, but avoids broker infrastructure and
algorithm changes.
