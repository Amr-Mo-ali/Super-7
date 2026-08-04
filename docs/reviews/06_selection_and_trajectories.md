# Selection and trajectories review — Phase 6

Scope: target selection, segment construction, ball reconstruction, camera motion, and
trajectory compensation. No thresholds/formulas were tuned and no production inference ran.

## Changes applied

- Added `tests/test_trajectory_contracts.py` to protect the no-op compensation invariant:
  raw coordinates remain unchanged and compensated values are explicitly unavailable.
- No canonical trajectory object was introduced. Existing overlapping representations have
  different ownership, and replacing them would alter multiple algorithms/response mappings.

## Findings

### Critical

None found.

### High

#### Camera estimator loads a complete selected segment into memory

`CameraMotionEstimator.estimate()` appends all selected grayscale frames to a list before
estimating transforms. This is inconsistent with the otherwise streaming video paths and can
consume excessive memory for long/high-resolution selections.

#### One rejected interval prevents every later compensated point

`estimate_frames()` sets `current = None` after any rejected interval. Later accepted
intervals require a non-`None` current transform, so no future transform is stored. This
properly avoids bridging a scene cut, but also permanently disables compensation after a
transient rejected interval instead of allowing a new independent run.

### Medium

#### Raw and compensated coordinates are not shared by movement/event consumers

`CompensatedObservation` provides the reusable raw/compensated point model. Movement,
segment-ball, pass, and shot calculations still consume their own raw forms
(`MovementPoint`, `BallTrackPoint`, observations, and coordinate tuples). The route reports
camera diagnostics but does not pass compensation into those consumers.

#### Duplicated incompatible trajectory forms exist

`BallTrackPoint`, `MovementPoint`, `PlayerObservation`, `BallObservation`, trajectory tuples,
and `CompensatedObservation` overlap. This is demonstrated duplication, but a canonical
replacement is not low risk: their optionality, timestamps, and owners differ, and each is
used by existing algorithms. Defer consolidation until compensation becomes a calculation
input rather than diagnostics-only.

#### Direct reconstruction contract does not validate FPS/range preconditions

`segment_ball.reconstruct()` divides by FPS while constructing points. API metadata validation
normally ensures valid FPS, but direct callers can supply zero/negative FPS or `end < start`.

#### Frozen result wrappers contain mutable dictionaries

`SegmentBallResult.points`/`quality_components` and several rejection diagnostic results are
mutable maps despite frozen outer dataclasses. This weakens data ownership/invariant safety.

### Low

- Segment construction sorts frames, deterministically splits long gaps/large jumps, and has
  no duplicate-key issue because its inputs are dictionaries.
- Ball reconstruction resolves same-frame multiple candidates deterministically, emits sorted
  points, and only interpolates short configured gaps with validated endpoints.
- Segment-ball quality weights are verified; segment-selection weights use exact float
  equality, brittle only if future literals change.
- Segment construction is linear after per-track sorting. `rejection_diagnostics` performs a
  filter across segments for each track (O(tracks × segments)); no clear O(n²) hot path.
- Camera interval frame numbering is adjacent and consistent; scene cuts are not bridged.

## Diagnostics consistency

Segment-ball exposes detected/interpolated/reconstructed counts and quality components.
Camera motion exposes interval counts/coverage/scene cuts separately. No typed record joins
raw, reconstructed, and compensated evidence, so clients must correlate multiple diagnostics.

## Exact modified files

- `tests/test_trajectory_contracts.py`
- `docs/reviews/06_selection_and_trajectories.md`

## Remaining risks

1. Non-streaming camera estimation memory use.
2. Compensation cannot resume after a rejected interval.
3. Raw/compensated evidence ownership is split.
4. Reconstruction preconditions are not locally enforced.
5. Mutable maps inside frozen result objects can be changed after analysis.
