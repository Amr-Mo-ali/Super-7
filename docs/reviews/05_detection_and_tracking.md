# Detection and tracking review — Phase 5

Scope: player/ball detector adapters, ByteTrack boundary/integration, tracking lifecycle,
conversion, and diagnostics. No threshold/model tuning or inference was performed.

## Changes applied

- Added inference-free adapter/tracker boundary tests in
  `tests/test_detection_tracking_contracts.py`.
- No production tracking behavior was changed. The main finding requires an explicit
  lifecycle decision rather than a local reset patch.

## Findings

### Critical

None found.

### High

#### ByteTrack state leaks between requests and is unsafe for concurrent requests

`main.create_app()` creates one `ByteTrackTracker`, passes it into one
`DetectionOnlyPlayerTracker`, and the application retains that tracker for the process
lifetime. `ByteTrackTracker` owns mutable `_tracker`, `_seen`, `tracks_created`,
`lost_tracks`, and `track_switches`; `DetectionOnlyPlayerTracker.analyze()` never resets it.

Consequences:

- IDs and `tracks_created` can depend on prior requests.
- Two concurrent requests can interleave `update()` calls into the same third-party tracker.
- Per-request diagnostics can contain cumulative tracker state.

The correct fix needs a clear per-analysis tracker ownership/factory decision. Adding only a
`reset()` call would not address simultaneous requests and is therefore not a safe isolated
change in this review.

### Medium

#### Both model adapters load independent YOLO instances

The player and ball adapters each call `YOLO(path)` in their constructors. With both paths
defaulting to `yolo11n.pt`, startup loads the same model weights twice. This is a resource
concern, not a functional adapter error; sharing requires a deliberate ownership/concurrency
policy and was not changed.

#### ByteTrack uses Ultralytics private, Results-like assumptions

`ByteTrackDetections` emulates attributes and indexing (`xyxy`, `conf`, `cls`, `xywh`,
`__getitem__`) expected by `ultralytics.trackers.byte_tracker.BYTETracker`. Tracker output is
assumed to have at least eight columns and to use column 4 as ID, 5 as confidence, and 7 as
source-detection index. This is version-coupled behavior with no pinned Ultralytics internal
API contract beyond the broad `>=8.3,<9` dependency range.

#### Ball detection failures are swallowed at video scope

`DetectionOnlyPlayerTracker.analyze()` catches every exception from ball detection/tracking,
sets a generic warning, and clears `ball_points`, then continues. This preserves a player
analysis, but discards the exception cause, frame index, and partial ball diagnostics. The
warning cannot distinguish model failure, malformed adapter output, or one bad frame.

### Low

#### Detection contracts are broadly consistent

Player and ball adapters emit frozen domain dataclasses with `BoundingBox`, frame index,
timestamp, and confidence. Player `Detection.track_id` is intentionally `None`; ByteTrack
later supplies IDs using `Track`. Ball uses `timestamp_seconds`, player uses `timestamp`;
the unit distinction is minor but inconsistent.

#### Array validation is strongest at the tracker boundary

`ByteTrackDetections` validates `(N,4)` shape, matched lengths, finite coordinates/confidence,
and positive box dimensions. Conversion is explicitly `float32`; empty payloads reshape to
`(0,4)` and are valid. The adapter layer assumes `result.boxes.xyxy.cpu().tolist()` and
`result.boxes.conf.cpu().tolist()` exist and are aligned; the player adapter does not reject
non-finite or degenerate model boxes, unlike the ball adapter.

#### Copies and frame iteration

`ByteTrackDetections.xywh` intentionally makes one copy to preserve `xyxy`. Slicing makes
typed `float32` copies. There is no full-video memory load: `DetectionOnlyPlayerTracker`
streams `VideoCapture` frame by frame and releases it in `finally`.

#### Diagnostics

Tracking diagnostics cover processed frames, detections, created track IDs, and ball counters.
`lost_tracks` and `track_switches` exist on `ByteTrackTracker` but are never updated or exposed,
so their names imply unavailable diagnostics. `TrackingDiagnostics.rejected_tracks` remains an
untyped dictionary record collection.

#### Tests and determinism

Adapter tests inject fake model objects; no YOLO/ByteTrack inference occurs in unit tests.
Existing API tests inject `FakeTracker`, yielding deterministic track IDs. There was no existing
test proving that two real tracker analyses are isolated; the High finding explains why one
would currently fail with a shared tracker.

## Exact modified files

- `tests/test_detection_tracking_contracts.py`
- `docs/reviews/05_detection_and_tracking.md`

## Remaining risks

1. Request-scoped tracker ownership is not enforced.
2. Third-party ByteTrack input/output internals may change within the accepted dependency range.
3. Player-adapter outputs need equivalent finite/degenerate-box validation.
4. Ball failures lose causal diagnostic detail.
5. Duplicate model objects can increase startup memory and GPU/CPU pressure.
