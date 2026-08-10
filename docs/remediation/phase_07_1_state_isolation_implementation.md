# Phase 7.1: Request-Local ByteTrack Implementation

## Previous ownership

`main.create_app()` constructed one `ByteTrackTracker` and injected that instance into
the application-scoped `DetectionOnlyPlayerTracker`. `ByteTrackTracker` owns mutable
`_tracker`, `_seen`, `tracks_created`, `lost_tracks`, and `track_switches`. Its lazy
Ultralytics `BYTETracker` therefore persisted across sequential analyses using the
same application instance.

## Implemented ownership

`DetectionOnlyPlayerTracker` remains application-scoped and now owns a factory, not a
tracker instance. Each `analyze()` call executes:

```python
tracker = self._tracker_factory()
```

The local `tracker` is used for the complete decode/frame-update loop and is never
assigned to an instance attribute. Consequently its counters, `_seen`, and lazy
Ultralytics `BYTETracker` are reachable only from that analysis invocation. The
existing `NearestNeighborBallTracker` remains constructed per `analyze()` call.

The application remains serial: `max_active_analyses = 1`. This phase does not make
YOLO concurrently safe and does not increase admission capacity.

## Files changed

| File | Change |
|---|---|
| `src/services/tracker.py` | Extended the existing tracker protocol with the three read-only diagnostics counters used by `DetectionOnlyPlayerTracker`. |
| `src/services/player_tracker.py` | Replaced the stored `ByteTrackTracker` dependency with `Callable[[], TrackerProtocol]`; constructs a local tracker at the start of each analysis. |
| `src/main.py` | Injects a typed factory that creates `ByteTrackTracker(resolved_settings)` for each analysis. |
| `tests/test_player_tracker_isolation.py` | Added deterministic fake detector/tracker/capture tests for sequential isolation, cancellation isolation, and unchanged request-local ball tracking. |

## Factory seam

The constructor dependency is:

```python
tracker_factory: Callable[[], TrackerProtocol]
```

`TrackerProtocol` is the pre-existing project-specific protocol. It declares `update`
and the diagnostics values read by the existing tracking response construction.
`main.create_app()` supplies a typed nested `tracker_factory()` that preserves the
same `Settings` passed to the previous shared instance. Lazy construction of the
underlying Ultralytics `BYTETracker` remains in `ByteTrackTracker._get_tracker()`.

## Request lifecycle

```text
create_app
  → detector wrappers + DetectionOnlyPlayerTracker + tracker factory

analyze request A
  → tracker_factory() → ByteTrackTracker A → lazy BYTETracker A
  → frame loop uses A only
  → returns / raises; A becomes unreachable

analyze request B
  → tracker_factory() → ByteTrackTracker B → lazy BYTETracker B
  → frame loop uses B only
```

If the factory raises, the exception is not caught in `analyze()`. It follows the
existing analysis error path and no request tracker is stored on application state.

## State-isolation proof

The new deterministic tests use a single `DetectionOnlyPlayerTracker` with a factory
that records produced fake trackers. They prove that two sequential analyses create
two different trackers; each receives only frames `[0, 1]`; each `_seen` set is
independent; and `tracks_created`, `lost_tracks`, and `track_switches` begin from the
new instance’s values. A cancelled first analysis produces a second, different tracker
for the next analysis. The ball-tracker test proves that the unchanged factory path
still creates one request-local ball tracker per analysis.

## Behavior-preservation proof

- `model_version` remains the same string assembled from the same settings paths and
  `bytetrack` suffix.
- The frame loop, detector calls, ByteTrack update algorithm, parameters, diagnostics
  formulas, ball behavior, warnings, selection, event detection, scoring, and V2
  mapping were not changed.
- `DEFAULT_MAX_ACTIVE_ANALYSES` remains `1`; admission code was not changed.
- Existing API tests continue to assert Public Rating V2 response behavior.

## Tests

Focused tests are run without YOLO, real ByteTrack, real video inference, GPU, or
network access. The new tests mock `cv2.VideoCapture` and use fake detector/tracker
objects.

| Command | Result |
|---|---|
| `uv run pytest -q tests/test_player_tracker_isolation.py tests/test_detection_tracking_contracts.py tests/test_ball_pipeline.py` | 14 passed |
| `uv run pytest -q tests/concurrency` | 22 passed |
| `uv run pytest -q tests/api` | 10 passed |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed; 171 files already formatted |
| `uv run mypy src tests` | failed with 9 errors in `tests/test_public_contract_stability.py`, `tests/test_event_arbitration.py`, and `tests/test_phase_00_safety_hardening.py`; no error references a Phase 7.1 changed file |
| `uv run pytest -q` | 201 passed |

## Migration risk

The constructor dependency changes from a tracker instance to a zero-argument factory.
Call sites must supply that factory. The repository call site is `main.create_app()`;
the new tests exercise direct construction with fake factories.

## Unresolved concurrency risks

- YOLO wrapper and underlying third-party model thread safety are not established by
  this implementation.
- Admission, artifact, and model ownership remain process-local.
- `asyncio.to_thread()` uses the runtime default executor; this phase does not change
  its size or scheduling.

## Exact next phase

The next phase is to characterize model-wrapper/thread safety and process-level
resource ownership before any increase to `max_active_analyses`. It must not increase
capacity until that characterization is complete.
