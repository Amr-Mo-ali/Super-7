# Super-7 analysis-latency workplan

> **Classification:** Proposed documentation-only workplan.
>
> **Authorization:** This document recommends only P1-A for a separate future
> implementation review. It does not authorize P1-B, later optimization slices,
> server access, benchmarking, deployment, or any runtime change.

## Purpose and evidence boundary

This workplan converts the approved read-only latency discovery into small,
independent future slices. Its purpose is to measure where current analysis time is
spent before considering optimization. It must not change scoring, numerical
semantics, target evidence, result quality, null handling, callback values, or the
one-worker execution policy.

Evidence labels used throughout:

- **Proven:** established directly by current source or deterministic tests.
- **Measured:** empirical evidence with its recorded environment and limitations.
- **Hypothesis:** plausible from source or prior evidence, but requiring current,
  same-video measurement before action.

Current source and tests take precedence over historical documents. The canonical
system boundary is summarized in the [handoff](../handoff/system-and-runtime.md), and
the detailed repository findings are recorded in the
[system audit](system-audit-2026-09-04.md).

## Current evidence summary

- **Proven:** the active spawned child performs one player prediction and one ball
  prediction for every decoded tracking frame. The calls are sequential and use
  separate lazy YOLO objects. The child and successfully loaded models persist
  across sequential requests.
- **Proven:** normal processing performs a complete private input copy, a validation
  open with one decoded frame, one full tracking decode, and a later complete
  SHA-256 file read.
- **Measured:** the historical short CPU benchmark attributed 47.49% of its pipeline
  time to player detection and 47.35% to ball detection. It used an obsolete request
  path and predates the current process and input-snapshot boundaries, so it is not
  a current baseline. See the
  [historical performance baseline](../remediation/phase_09_1_performance_resource_baseline.md).
- **Measured:** supplied VPS observations provide whole-analysis, callback and
  end-to-end durations for two different videos, plus one CPU/memory observation.
  They do not attribute current stages or constitute a cold/warm comparison. See
  [production evidence](../handoff/production-evidence-and-operations.md).
- **Hypothesis:** player and ball inference remain the dominant current cost. P1-A
  and P1-B must establish this on the current execution path before model or decode
  optimization is considered.

## Current end-to-end stage map

Timing abbreviations used below are `A` for `admission_duration_ms`, `Q` for
`queue_wait_ms`, `X` for `analysis_duration_ms`, `P` for response
`processing_time_ms`, `C` for `callback_duration_ms`, and `E` for
`end_to_end_duration_ms`.

| Stage | Owner, behavior and frequency | Current timing inclusion |
|---|---|---|
| Admission | Parent event-loop thread; request/reference checks and synchronous callback DNS validation; once per request | `A` only; enqueue occurs afterward, so excluded from `Q`, `X`, `P`, and `E` |
| Queue wait | Parent `asyncio.Queue`; asynchronous wait from enqueue to claim; once per request | Measured by `Q`; included in `E` |
| Worker claim/start | Parent event-loop worker task; state bookkeeping and lifecycle log; once per request | No exclusive timer; included in `E`, mostly before `X` |
| Process submission/startup | Parent submits a pickle-safe request and asynchronously waits; spawned child starts lazily on the first submission | No separate timer; cold startup is included in `X` and `E`, not `P` |
| Child initialization | Child main thread constructs lazy components, resolver and artifact manager; once per child | Initialization event only; cold time included in `X` and `E` |
| Model initialization | Child, synchronous within each model's first inference; once per successfully loaded model per child | Player load has a timer, ball load does not; included in `P`, `X`, and `E` |
| Resolution/session/materialization | Child, synchronous path resolution, artifact-session creation and complete bounded private-file copy; once per request | No exclusive stage timer; included in `P`, `X`, and `E` |
| Validation | Child, synchronous metadata access and first-frame decode; once per request | Only available through inactive opt-in profiling; included in `P`, `X`, and `E` |
| Main decode and tracking | Child, sequential full-video decode; two model calls, ByteTrack update and ball tracking per frame | Per-frame inference logs exist, but no active aggregate breakdown; included in `P`, `X`, and `E` |
| Reproducibility hashing | Child, synchronous full-file SHA-256 read after tracking; once per request | No timer; included in `P`, `X`, and `E` |
| Target/segment resolution | Child, synchronous dominance evaluation and winning-track segment construction; once after tracking | Selection timer exists; included in `P`, `X`, and `E`; unavailable results exit here |
| Camera motion | Child, debug-only range/prefix decode plus feature/optical-flow work; established targets only | Partial internal timer omits most decode/grayscale work; included in `P`, `X`, and `E` |
| Post-processing and scoring | Child, reconstruction, proximity, pass, shot, movement, interaction, technical events and provisional scoring; once for an established target | Several overlapping internal timers; included in `P`, `X`, and `E` |
| Response construction | Child creates the internal Pydantic response | No exclusive timer; `P` ends at an inconsistent point within this boundary |
| Debug rendering | Child, debug-only full decode, overlays and optional video/JPEG output | No timer; occurs after `P` is fixed, so included only in `X` and `E` |
| Child serialization and IPC | Child JSON serialization and pickled result envelope; parent JSON validation | No exclusive timers; included in `X` and `E`, excluded from `P` |
| Parent mapping | Parent event loop performs arbitration, public projection and detailed-rating mapping | No exclusive timer; included in `X` and `E` |
| Callback serialization/delivery | Parent serializes callback; blocking transport runs in a thread; retry sleeps are asynchronous | Attempts and total `C` measured; excluded from `X`/`P`, included in `E`; occupies the sole worker slot |
| Terminal update | Parent event loop updates the in-memory job state after callback completion/exhaustion | No exclusive timer; `E` is captured during terminal bookkeeping |

The active ownership and timing boundaries are defined by
[`main.py`](../../src/main.py),
[`analysis_queue.py`](../../src/services/analysis_queue.py),
[`process_analysis_pool.py`](../../src/services/process_analysis_pool.py),
[`process_entrypoint.py`](../../src/services/process_entrypoint.py), and
[`routes.py`](../../src/api/routes.py).

## Complete video and file-pass inventory

| Boundary | Scope and access pattern | Required state | Duplication or dependency |
|---|---|---|---|
| Private input materialization | Complete sequential byte copy in bounded chunks; not a decode | Every request | Safety/ownership boundary; do not remove |
| Validator `VideoCapture` | Opens at frame zero, reads metadata and one frame; no seek | Every request | Reopens before the main decode |
| Tracker `VideoCapture` | Sequentially reads every decodable frame through EOF; no sampling or seek | Every request | Primary full decode and inference pass |
| Reproducibility SHA-256 | Complete sequential byte read; not a decode | Every request after tracking | Duplicates file I/O already performed by materialization |
| Debug source copy | Complete `copyfile`; not a decode | Debug media enabled | Happens before target availability is known |
| Camera-motion `VideoCapture` | Reads from frame zero through the selected inclusive end; frames before selected start are decoded and discarded | Debug enabled and target established | Re-decodes frames already processed by tracking |
| Debug-render `VideoCapture` | Sequentially reads the complete video; optional encode and per-frame JPEG writes | Debug enabled and target established | Re-decodes the complete source |

A normal debug-disabled request therefore has one first-frame validation decode and
one full tracking decode. An established debug-enabled request can add a selected
prefix/range decode and another full render decode. These passes have different
ownership, failure, range and artifact semantics; no workplan slice may assume they
can be combined safely.

Relevant boundaries are implemented in
[`artifacts.py`](../../src/diagnostics/artifacts.py),
[`video_validator.py`](../../src/services/video_validator.py),
[`player_tracker.py`](../../src/services/player_tracker.py),
[`reproducibility.py`](../../src/core/reproducibility.py),
[`camera_motion.py`](../../src/services/camera_motion.py), and
[`debug_renderer.py`](../../src/services/debug_renderer.py).

## Model initialization and inference inventory

| Component | Initialization and lifetime | Invocation/configuration | Reuse and evidence |
|---|---|---|---|
| Player YOLO, COCO class 0 | Separate lazy object; loaded on first player inference and retained for child lifetime | One prediction per frame; default image size 640, batch 1, configured device default `cpu` | Output reused by player tracking; historical short benchmark measured it as a major contributor |
| Ball YOLO, COCO class 32 | Separate lazy object; loaded on first ball inference and retained for child lifetime | One prediction per frame; default image size 640, batch 1, same configured device | Output reused by ball tracking; historical short benchmark measured it as a major contributor |
| ByteTrack | Non-model state created once per request and updated sequentially per frame | Detection-array association using configured thresholds and buffer | State cannot be batch-reordered without changing identity behavior |
| Camera motion | OpenCV feature/optical-flow algorithm, not a learned model | Original debug frames converted to grayscale | Diagnostic-only in the active route; does not alter movement scoring |
| Event/scoring services | Deterministic rule-based code, not learned models | Operate on retained observations and candidates | No weights or accelerator lifecycle |

The player adapter's `detect_batch` currently loops over individual `detect` calls;
it is not true batching and is not active. Successful models do not reload per
request. A persistent ball initialization failure may be retried on later frames
because ball exceptions are handled inside the per-frame loop; changing this requires
a separately approved failure-policy decision.

Default model paths may be identical, but that does not prove combined inference is
equivalent. Player and ball calls use different class filters and confidence settings,
and any detection difference can propagate through track identity, target selection,
events and scores. See
[`yolo_player_detector.py`](../../src/adapters/yolo_player_detector.py),
[`yolo_ball_detector.py`](../../src/adapters/yolo_ball_detector.py), and
[`tracker.py`](../../src/services/tracker.py).

## Current timing meanings, overlaps and gaps

Current meanings:

- `admission_duration_ms` covers request admission validation through queue outcome.
- `queue_wait_ms` covers enqueue through the `RUNNING` transition.
- `analysis_duration_ms` covers parent processor start through child completion, IPC
  validation and parent callback projection; it excludes callback delivery.
- `callback_attempt_duration_ms` covers one callback attempt.
- `callback_duration_ms` covers callback validation/serialization, attempts and retry
  waits.
- `end_to_end_duration_ms` covers enqueue through callback handling and terminal-state
  bookkeeping.
- response `processing_time_ms` covers an incomplete child-only interval and must not
  be treated as whole-analysis duration.

Known overlaps and inconsistencies:

- player-detection and tracking response fields contain the same combined duration;
- ball processing encloses pass and shot timers;
- controlled-movement and dribble timing fields both contain the full technical-event
  duration;
- camera-motion timing excludes most decode and grayscale conversion;
- available-result `processing_time_ms` ends before debug rendering, child
  serialization, cleanup, IPC and parent mapping;
- the child envelope records a processing duration, but parent validation discards it;
- first-frame inference duration overlaps lazy model initialization;
- per-frame INFO logs can perturb the work being measured;
- the application declares models initialized while production model construction is
  still lazy.

Missing current boundaries are materialization, hashing, child startup, ball-model
load, aggregate decode/inference/tracker work, camera decode, movement, response
construction, debug rendering, child serialization, IPC/parent validation, parent
mapping and cleanup. Parent/child peak RSS, native thread counts and GPU utilization
are also not captured reliably by the active production path.

## Complexity and resource classification

Let `F` be decoded frames, `P` pixels per frame, `T` visual tracks, `S` the selected
segment span, `I` interaction/controlled candidates, and `E` arbitration candidates.

| Stage | Approximate scaling and retained memory | Dependency | Primary concern |
|---|---|---|---|
| Materialization and hash | Two `O(file bytes)` passes today; bounded copy buffer | Disk/page cache | Latency, observability |
| Validation/decode | First-frame validation plus main `O(F * P)` decode | Codec, CPU, disk | Latency |
| Player/ball inference | Two model calls per frame; model-dependent scaling with `F` and image size | CPU/GPU | Latency, throughput |
| Tracking | Per-frame association; boxes, confidences, ball points and candidates grow with duration | CPU, RAM | Latency, long-video memory |
| Target/segments | Frame sorting and segment construction, approximately `O(observations log observations)` | CPU | Correctness, latency |
| Camera motion | `O(range * pixels/features)`; live pixel frames bounded, transform metadata grows with range | CPU | Debug latency, memory |
| Pass/shot | Independently rescan ball frames and tracks, approximately `O(S * T)` plus bounded windows | CPU | Latency |
| Movement/interaction | Sorting/indexing plus selected-span work, approximately `O(S log S)` | CPU, RAM | Latency |
| Technical events | Interaction-span work; dribble analysis can rescan movement per controlled candidate, up to `O(I * S)` | CPU, RAM | Latency |
| Arbitration | Pairwise conflict grouping, `O(E^2)` | CPU | Latency |
| Debug render | Full decode/optional encode; accumulated trajectory redraw can approach `O(F^2)` drawing work | CPU, disk | Debug latency |
| Serialization/IPC | `O(serialized response bytes)` across several transformations | CPU, RAM, IPC | Latency |
| Callback | Network attempts plus configured retry waits | Network, thread pool | End-to-end latency, throughput |

The known short smoke fixture is suitable only for process lifecycle and cleanup
verification. It cannot prove long-video memory behavior or production throughput.

## Safe structured logging policy

New performance records may include only:

- generated analysis correlation ID;
- fixed allowlisted stage name and instrumentation schema version;
- aggregate duration and bounded scalar counters;
- success, unavailable, failed or skipped outcome;
- parent/child role, PID and cold/warm marker;
- opaque commit, image, model and safe-configuration digests.

They must not include:

- video, model, debug or filesystem paths;
- video filenames or contents;
- callback URLs, IP addresses, host/server addresses or response bodies;
- tokens, credentials, payloads or exception messages;
- `playerId`, user-linked `videoId`, or other user data;
- per-frame timing arrays, boxes, coordinates or image data.

Diagnostic evidence must remain outside the production Git checkout.

## New-server frozen-manifest and comparison plan

Execution is deferred until new-server access and separate authorization exist. For
the future comparison, store evidence outside the repository and freeze:

- input SHA-256;
- container and stream codec, pixel format, frame count, FPS, dimensions and duration;
- full Super-7 commit SHA;
- immutable container image digest;
- SHA-256 for both configured model files;
- Python, OpenCV, Ultralytics, Torch and codec-library versions;
- hardware, operating system, container CPU/RAM limits and accelerator/driver;
- safe digest of effective settings, football profile, shot/camera configuration,
  debug policy and native-thread environment;
- queue capacity, callback timeout and instrumentation schema version.

The configuration digest must exclude secrets and raw paths.

Use the same immutable representative video for baseline and candidate comparisons.
A cold run starts a fresh application and spawned child and records process startup
and both first model loads. Repeated warm runs reuse the same long-lived child and
loaded models, and each begins only after the prior job reaches terminal state.
“Cold” describes process/model state, not an unverified operating-system page-cache
state.

Keep input, commit, image, model files, configuration, debug-disabled policy and
callback target identical. Record admission, queue wait, every approved aggregate
stage, analysis duration, callback duration, terminal end-to-end duration, frame and
candidate counts, parent/child peak RSS, CPU/native threads, GPU utilization/memory
when available, and artifact/disk bytes. Compare early-unavailable and established
full-analysis behavior separately.

### Correctness gates

Canonicalize only approved volatile timing and generated identity fields. A candidate
must preserve:

- the same target evidence and selected target/segment state;
- the same accepted and rejected candidates;
- the same scores and confidence values;
- the same null-versus-zero states;
- the same callback field presence and values except approved volatile timing and
  generated identity fields.

Any unexplained difference blocks the optimization. No latency improvement permits a
silent result-quality or numerical-semantics change.

## Corrected independent future slices

### P1-A — coarse stage timing only

Measure aggregate duration at the existing orchestration boundaries for:

- materialization;
- validation;
- tracking total;
- hashing;
- target/segment resolution;
- post-processing/scoring;
- debug work or an explicit skipped state;
- child serialization;
- process/IPC and parent validation;
- parent mapping;
- cleanup.

Requirements:

- use a monotonic high-resolution clock;
- retain one aggregate scalar per stage and no per-frame timing arrays;
- keep output in internal structured logs/diagnostics only;
- make no public response or callback schema change;
- emit safe allowlisted fields only;
- use fake-clock tests covering success, unavailable and failure paths;
- document inclusive, exclusive, nested and skipped timing relationships;
- do not modify player or ball detector internals.

Rollback boundary: one independent instrumentation commit. Acceptance requires
unchanged analysis and callback output plus internally consistent stage definitions;
stages must not be assumed additive when explicitly documented as nested.

### P1-B — tracking timing breakdown

Only after P1-A is green and separately authorized, break tracking total into aggregate
video decode, player inference, ball inference, ByteTrack update and ball tracking.

Use bounded scalar accumulators only. Do not change results, numerical behavior, model
invocations, frame ownership, frame copying or per-frame INFO volume. Aggregate values
must reconcile with tracking total within documented instrumentation overhead.

### P2 — measured unnecessary-work removal

Only after measurement, make one independently reviewed change per commit. Candidate
examples are copy-time hashing, bounded inference-log aggregation, or duplicate
internal diagnostic-calculation removal. No change proceeds unless its measured stage
is material. Every change must pass the correctness gates above and retain a direct
rollback boundary.

### P3 — model lifecycle

Consider cold/warm model-initialization optimization only after stage evidence proves
it material. Preserve failure behavior, process ownership, model identity, output
equivalence and one-worker policy.

### P4 — batching/decoding

Consider batching or decoding optimization only after current/new-server evidence
proves detection dominates and exact behavioral equivalence can be maintained. Preserve
every frame, order, timestamp, class-specific threshold, ByteTrack update, target,
candidate and numerical result.

### P5 — immutable same-video server comparison

Execute the frozen-manifest cold/warm comparison only after new-server access and
separate authorization. Compare immutable baseline and candidate images and retain the
prior image digest as the rollback boundary.

Callback lifecycle separation belongs to Sprint 2 durability. It must not be
implemented inside this performance workplan.

## Explicitly rejected premature optimizations

Before the required evidence and separate approval, this workplan rejects:

- model replacement;
- GSR implementation;
- frame skipping or sampling;
- image-size, confidence or IoU threshold changes;
- worker/process-count increase;
- private input-snapshot removal;
- unproven combination of player and ball detection.

It also authorizes no scoring, rating, target-selection, identity, null-policy,
callback-schema, numerical, durability, deployment or infrastructure change.

## First future implementation recommendation

Recommend **P1-A only** for a separate implementation review.

This document does not authorize P1-A implementation. P1-B, P2, P3, P4 and P5 remain
deferred and require their own evidence and human authorization.

## P1-A implementation verification — 2026-09-16

### Classification

P1-A coarse analysis-stage timing is implemented and locally verified. This is
observability instrumentation, not a latency optimization, and no claim is made that
analysis is faster. P1-B through P5 remain deferred and unauthorized. Current
production latency attribution still requires deployment to the correct server and
representative controlled evidence.

### Active execution ownership

The default application path is:

```text
AnalysisWorker
→ ProcessAnalysisPool
→ ProcessPoolExecutor(max_workers=1, spawn)
→ run_child_analysis
→ parent validation/mapping
→ callback delivery
```

Child stages use a child-process-local monotonic clock. Parent stages use a
parent-process-local monotonic clock. Timestamps from different processes are never
subtracted, and durations from nested boundaries must not be added together.

### Implemented stages

The child-owned stages are:

```text
input_materialization
validation
tracking_total
reproducibility_hashing
target_segment_resolution
post_processing_scoring
debug_work
child_serialization
child_cleanup
```

The parent-owned stages are:

```text
process_ipc_parent_validation
parent_response_mapping
```

### Timing relationships

`process_ipc_parent_validation` is inclusive of child work, serialization, transport
and parent validation. It is not additive with child-stage durations.
`post_processing_scoring` wraps `_completed()`. `debug_work` is a disjoint accumulator
for debug-source preparation, camera motion and render/publication. Camera motion and
render/publication occur within post-processing, so `debug_work` can overlap
`post_processing_scoring`. A skipped debug path emits `outcome=skipped` with zero
duration. Unavailable, failed, cancelled, success and skipped outcomes are emitted
only where truthful.

### Safe log contract

Each timing record contains exactly:

```text
analysis_id
timing_schema_version
stage
role
outcome
duration_ms
```

Timing records exclude player and video identifiers, filenames and filesystem paths,
callback URLs, payloads, exception messages, cleanup details, boxes, coordinates,
images, frames and per-frame arrays. Timing logs do not enter public responses or
callback payloads.

### Behavior preservation

P1-A preserves analysis results, target evidence, scoring values, null-versus-zero
semantics, callback output, exact direct exception identity, sanitized child failure
behavior, cancellation classification, cleanup execution and precedence, artifact
publication and retention behavior, and debug-disabled behavior.

### Logging correction

Timing emission calls only the supplied logger and does not mutate global
propagation. The test capture utility chooses one capture route and restores handler
identity, handler order and logger state on context exit. A timing logging failure
cannot replace the application result or primary exception.

### Persistent verification evidence

```text
P1-A collection:                 10 tests
P1-A contract:                   10 passed
capture_application_logs users: 61 passed
focused regressions:             118 passed
broader regressions:             153 passed
F08:                             13 passed
F14-A:                            5 passed
F14-B:                           18 passed
F19:                              9 passed
complete offline suite:          507 passed, 1 skipped
```

The accepted skip was:

```text
tests/test_video_path_resolver.py:48
Windows symlink creation unavailable: WinError 1314
```

Static verification recorded `mypy src tests` with 185 files and no issues, and
`mypy src` with 105 files and no issues. Ruff check passed; Ruff format check reported
287 files already formatted. Syntax compilation, affected import smoke, UTF-8 checks
and whitespace checks passed.

### Local-only status

This implementation has not been pushed, merged, deployed or benchmarked on the new
production server. No inference or real-video benchmark was performed. The next
performance step remains controlled measurement using P1-A evidence. P1-B requires
separate authorization after real measurements identify the dominant cost.

The intended local commit subject is
`feat(observability): add coarse analysis stage timing`. The final object ID belongs
in post-commit evidence and is intentionally not recorded inside this commit.
