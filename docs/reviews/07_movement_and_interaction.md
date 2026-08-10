# Movement and interaction review — Phase 7

Scope: movement, smoothing, speed/acceleration, direction/stationary metrics, ball proximity,
interaction segmentation, confidence, and quality. No formulas or thresholds were changed.

## Changes applied

- Added `tests/test_movement_interaction_invariants.py` with bounded-metric and
  insertion-order invariants.
- Updated `NormalizedBallProximityAnalyzer` to sort frame mappings before segmentation. This
  preserves ordinary ordered-input behavior while making equivalent mappings deterministic.

## Findings

### Critical

None found.

### High

#### Ball-proximity segmentation previously depended on dictionary insertion order — resolved

`NormalizedBallProximityAnalyzer.analyze()` iterates `ball_points.items()` without sorting,
then `_segments()` assumes ascending frames. Equivalent frame mappings inserted in a different
order could split or merge segments differently. Frame iteration now sorts keys before
segmentation; the new property-style test protects that invariant.

### Medium

#### Movement uses raw image coordinates only despite camera diagnostics

`BottomCenterMovementAnalyzer` derives every position, distance, speed, acceleration, and
direction from raw bounding-box bottom centers. Camera compensation is neither injected nor
represented in its result. The public warnings correctly describe image-space limitations, but
the route-level `movement_metrics_source` remains raw even when camera diagnostics exist.

#### Geometry helpers are duplicated

Euclidean distance/bottom-center normalization appears in movement, ball proximity,
interaction evidence, segment selection, pass/shot, and reconstruction. Their denominator and
coordinate choices differ legitimately, so a generic helper would obscure semantics; however,
the duplication makes unit/rounding consistency difficult to audit.

#### Diagnostics do not record dropped non-positive time intervals

Movement silently skips non-positive timestamp deltas after trajectory construction. In normal
input timestamps derive from monotonic frame/fps, but direct callers can supply duplicate or
non-monotonic frames only indirectly through mappings. No counter explains such dropped
intervals. Jump rejections are counted; invalid box-height observations are silently omitted.

#### Repeated scans occur but no apparent quadratic hot path

Movement builds distances, speeds, vectors, stationary intervals, accelerations, and angles in
separate linear passes. Interaction builds indexes/evidence, then makes separate aggregates for
diagnostics and segments. These are O(n), with readability benefits; they may become relevant
only after profiling long clips.

### Low

#### Time/unit handling

Movement timestamps are `frame / fps`; speed is image pixels/second and acceleration is image
pixels/second². `MovementMetrics` field names omit those units, while API feature names mostly
include them. Stationary duration is seconds; `stationary_frames` uses rounded duration × FPS,
so it is an estimate rather than an exact observed-frame count.

#### Jitter handling

Movement rejects large normalized jumps and applies a trailing moving average. The trailing
window introduces a phase lag and can suppress/alter short turns. Direction changes ignore
short vectors. These are documented heuristic choices; no threshold tuning was performed.

#### Interaction safety/quality

Interaction input indexing rejects duplicate selected-player and accepted-ball frame indices.
Evidence marks invalid/non-finite geometry as missing, bridges only configured missing evidence,
and does not bridge `non_candidate` evidence. Confidence validates weights/scales and clamps
components. `_validate` reconciles segment accounting, ordering, coverage/confidence bounds,
and bridged-frame counts. This is one of the strongest invariant boundaries in the pipeline.

## Exact modified files

- `src/services/ball_proximity.py`
- `tests/test_movement_interaction_invariants.py`
- `docs/reviews/07_movement_and_interaction.md`

## Remaining risks

1. Movement remains raw image-space despite camera-motion diagnostics.
2. Some invalid observations/intervals are silently omitted rather than diagnosed.
3. Pixel-derived values can be misconstrued as physical movement unless clients honor warnings.
