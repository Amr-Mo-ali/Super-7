# Phase 7: Request-State Isolation

## Objective

Eliminate cross-request mutable tracking state without changing detection, tracking
logic, scoring, V2 mapping, HTTP contracts, configuration values, or execution order.

This is an ownership migration plan. No runtime code is changed by this document.

## Evidence and finding

`main.create_app()` constructs one `DetectionOnlyPlayerTracker` and passes it one
`ByteTrackTracker` (`src/main.py`). `DetectionOnlyPlayerTracker.analyze()` calls
`self._tracker.update(...)` for every decoded frame (`src/services/player_tracker.py`).
`ByteTrackTracker` retains `_tracker`, `tracks_created`, `lost_tracks`,
`track_switches`, and `_seen` as instance attributes (`src/services/tracker.py`).
Its underlying Ultralytics `BYTETracker` is created on the first update and is never
reset by `DetectionOnlyPlayerTracker.analyze()`.

Therefore the same mutable ByteTrack object is used by successive requests handled by
one application instance. The current process-local admission limit of one serializes
access but does not make the tracker request-local.

`NearestNeighborBallTracker` is already constructed inside each `analyze()` call and
is request-local. The analysis services below retain only their injected configuration
or create working collections inside method calls.

## Scope definitions

| Scope | Definition in this repository |
|---|---|
| Process | One Python process and its module imports, default event-loop executor, and process-local filesystem/runtime state. |
| Application | One `FastAPI` object returned by `create_app()` and the collaborators attached to it or closed over by its router. |
| Request | One accepted `/analyze` lifecycle identified by the generated UUID, from lifecycle setup through its `finally` cleanup. |
| Invocation | One function/method call and its local variables, temporary collections, decoded frame, or return value. |

## Ownership table

### Composition, API, concurrency, and diagnostics

| Object / module | Current scope | Mutable state and evidence | Required scope after migration |
|---|---|---|---|
| `Settings`, `DebugSettings`, immutable config dataclasses | Application | Frozen dataclasses; supplied at `create_app()` | Application |
| FastAPI app, router closure, logger objects | Application | Created by `create_app()` and captures injected collaborators | Application |
| `AdmissionController` | Application | Process-local counters protected by `Lock` | Application |
| `AnalysisExecutor` | Application | No instance fields; delegates each call to `asyncio.to_thread()` | Application |
| `RequestLifecycle` | Application | References admission, executor, and artifact manager | Application |
| `ArtifactManager` | Application | Session/retention maps and ordering counter protected by `Lock` | Application |
| `AdmissionPermit` | Request | One release flag and a reference to the application controller | Request |
| `CancellationManager`, `CancellationChecker` | Request | Request ID, cancellation state, event, and lock | Request |
| `ArtifactSession`, `ArtifactReservation` | Request | Request directory, reservation/finalization state, and session lock | Request |
| `temporary_upload` file and `UploadFile` | Request | Created and closed by the async context manager | Request |
| route response models, public V2 mapping working data | Invocation | Constructed/mapped for one route call | Invocation |

### Detection and tracking

| Object / module | Current scope | Mutable state and evidence | Required scope after migration |
|---|---|---|---|
| `YOLOPlayerDetector` and its Ultralytics `YOLO` object | Application | Constructed by `create_app()`; wrapper stores settings/logger/model and does not assign them during `detect()` | Application |
| `YOLOBallDetector` and its Ultralytics `YOLO` object | Application | Constructed by `create_app()`; wrapper stores settings/logger/model and does not assign them during `detect()` | Application |
| `DetectionOnlyPlayerTracker` | Application | Stores detector, tracker, settings, ball detector, and ball-tracker factory | Application, but it must no longer own a tracker instance |
| `ByteTrackTracker` | **Application** | Stores lazy `_tracker`, `_seen`, and counters; supplied by `main.create_app()` | **Request** |
| Ultralytics `BYTETracker` held by `ByteTrackTracker._tracker` | **Application after first use** | Lazy-created by `_get_tracker()` and retained by the app-owned wrapper | **Request** |
| `NearestNeighborBallTracker` | Request | Created in `DetectionOnlyPlayerTracker.analyze()`; stores last detection, missing count, segment, history, and counters | Request |
| `cv2.VideoCapture` used for tracking | Invocation | Opened in `analyze()` and released in its `finally` | Invocation |
| decoded frame, detections, tracking dictionaries/lists, `TrackingRun` | Invocation | Local variables of `analyze()`; returned run remains reachable until pipeline completion | Invocation |
| `VideoValidator` | Application | Stores only settings | Application |
| validator `cv2.VideoCapture` and metadata | Invocation | Opened/released inside `validate()` | Invocation |

### Analysis services

All objects in this table either store immutable configuration only or create mutable
working state within an invocation. None has instance fields updated by its analysis
method in the repository.

| Service/module | Current scope | Required scope |
|---|---|---|
| `NormalizedBallProximityAnalyzer` | Application | Application |
| `BottomCenterMovementAnalyzer` | Application | Application |
| `BallInteractionAnalyzer` | Application | Application |
| `TechnicalEventAnalyzer` | Application | Application |
| `PassDetector` | Application | Application |
| `ShotDetector` | Application | Application |
| `RuleBasedPhysicalActivityScorer` | Application | Application |
| `WeightedTargetPlayerSelector` | Application | Application |
| `FeatureExtractor` | Application | Application |
| `CameraMotionEstimator` | Invocation; constructed in `_completed()` when a debug source exists | Invocation |
| `TechnicalScorer` | Invocation; constructed in `_completed()` | Invocation |
| `render_debug_video`, `reconstruct`, segment selection, trajectory compensation, tracklet stitching | Invocation functions | Invocation |
| event arbitration and player-rating services | Not constructed or invoked by the `/analyze` composition path | Their instances store configuration only; application scope when composed, invocation scope for their local results |
| service protocols, schemas, frozen dataclasses, constants, and `__init__` re-exports | Module/type definitions | Process for module constants; invocation for instantiated result values |

The listed analysis services use local lists, dictionaries, deques, counters, result
objects, and OpenCV arrays during calls. Those values are invocation-scoped. This is
not a claim that third-party libraries have a particular thread-safety contract; the
repository contains no such contract.

## Dependency graph

### Current ownership

```text
create_app() [application]
  ├─ YOLOPlayerDetector [application]
  ├─ YOLOBallDetector [application]
  ├─ ByteTrackTracker [application, mutable]  ◄── cross-request state
  └─ DetectionOnlyPlayerTracker [application]
       ├─ player detector [application]
       ├─ ByteTrackTracker [application, shared]
       ├─ ball detector [application]
       └─ analyze(request)
            ├─ VideoCapture [invocation]
            └─ NearestNeighborBallTracker [request]
```

### Target ownership

```text
create_app() [application]
  ├─ YOLOPlayerDetector [application]
  ├─ YOLOBallDetector [application]
  └─ DetectionOnlyPlayerTracker [application]
       ├─ player detector [application]
       ├─ tracker factory [application; creates no tracker at startup]
       ├─ ball detector [application]
       └─ analyze(request)
            ├─ ByteTrackTracker [request]
            │    └─ lazy Ultralytics BYTETracker [request]
            ├─ NearestNeighborBallTracker [request]
            └─ VideoCapture and frame collections [invocation]
```

The player and ball detector objects remain application-scoped because the repository
constructs them once at the composition root and their wrappers do not update instance
state during inference. This phase moves only the demonstrated mutable tracker state.

## Target lifecycle

```text
application construction
  → construct immutable configuration, detector wrappers, analysis services,
    lifecycle objects, and a ByteTrack factory

accepted request
  → create cancellation manager and artifact session
  → worker invocation starts
  → `DetectionOnlyPlayerTracker.analyze()` creates one ByteTrackTracker
  → first frame requiring tracking lazily creates that request's BYTETracker
  → all frame updates use only that request's tracker and ball tracker
  → TrackingRun returned; local trackers become unreachable when invocation ends
  → artifact cleanup, cancellation completion, permit release, upload cleanup

next accepted request
  → creates a different ByteTrackTracker and different BYTETracker
```

## Migration plan

1. Add a request-tracker factory seam to `DetectionOnlyPlayerTracker`. The factory
   receives the same `Settings` currently passed to `ByteTrackTracker`; it returns a
   new `ByteTrackTracker` for each `analyze()` invocation.
2. Replace the constructor-held `ByteTrackTracker` dependency with that factory. At
   the start of `analyze()`, create one local tracker before the decode loop and use
   that local object for every frame in the request.
3. Update `main.create_app()` to inject a factory for `ByteTrackTracker` rather than
   constructing the tracker instance at application composition time. Keep detector,
   ball-detector, settings, lifecycle, response mapping, and all algorithm arguments
   unchanged.
4. Preserve `DetectionOnlyPlayerTracker.model_version` exactly. It is currently
   derived from configured player/ball paths and the literal `bytetrack`, not tracker
   instance state.
5. Add deterministic characterization tests using fake trackers that run two analyses
   through the same `DetectionOnlyPlayerTracker` and assert that the factory is called
   twice and that neither tracker receives updates from the other request.
6. Retain existing unit, integration, API, V2 contract, scoring, and concurrency
   tests. No thresholds, result schemas, response mapping, or pipeline stage ordering
   change.

## Acceptance criteria

- A `ByteTrackTracker` is created within each `DetectionOnlyPlayerTracker.analyze()`
  invocation.
- Its lazy Ultralytics `BYTETracker`, `_seen`, and counters cannot be reached by a
  later request.
- The ball tracker remains one instance per analysis invocation.
- Application-scoped detector wrappers and stateless/configuration-only services
  retain their existing construction and call behavior.
- Public Rating V2 serialization and all current pipeline results for one isolated
  request are unchanged.
