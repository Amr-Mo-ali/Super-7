# Phase 8B — Pass and Shot Detection Review

**Scope:** pass detection, shot detection, shared release and trajectory evidence,
receiver lookup, and preparation/follow-through evidence.

**Review constraint:** no event-classification logic was added and no acceptance
threshold, formula, or outcome was changed. No production video inference was run.

## Files reviewed

- `src/services/pass_detection.py`
- `src/services/shot_detection.py`
- `src/api/routes.py`
- `src/services/debug_renderer.py`
- `src/core/config.py`
- `tests/integration/test_pass_detection.py`
- `tests/integration/test_shot_detection.py`

## Event flow

Both analyzers are constructed by the API composition root and invoked after ball
analysis with the same player boxes, player confidences, raw ball points, and FPS.
They independently construct possession windows, evaluate release evidence, extract
future ball trajectories, and then make their respective decisions:

```text
ball/player observations
  ├─ PassDetector: possession → release → trajectory → receiver → pass decision
  └─ ShotDetector: possession → preparation → release → trajectory → follow-through
                                                    → shot decision
```

They do not currently exchange evidence or reconcile overlapping accepted events.

## Findings

### Critical

None found within this scope.

### High

1. **Release and trajectory evidence are duplicated independently.**
   `PassDetector` and `ShotDetector` each contain substantially equivalent
   possession grouping, release-window scanning, and future-trajectory extraction.
   The implementations have already diverged in configuration ownership and output
   shape. A fix or edge-case adjustment made in one can silently make the two event
   decisions inconsistent for identical input evidence.

   A future, bounded extraction is justified: immutable `ReleaseEvidence` and
   `TrajectoryEvidence` domain records plus narrowly scoped pure extraction helpers.
   The helpers must retain the current analyzer-specific quality calculations and
   return the same intermediate values, rather than becoming an event engine or
   introducing event classification. This was not extracted in this review because
   preserving all acceptance outcomes first requires characterization tests for
   cross-analyzer parity on boundary cases.

2. **Rejected candidates are represented only as aggregate counters.**
   Both result objects retain accepted event records but discard the frame range,
   partial evidence, and specific reason for each rejected candidate. The API can
   report a rejection breakdown but cannot answer which possession was rejected,
   why, or render it for diagnosis. This makes false-negative investigation and
   regression comparison unnecessarily difficult.

   Preserve rejected records as typed diagnostic candidates; do not promote them to
   accepted response events or alter the accepted-candidate contract.

### Medium

1. **Pass/shot overlap is not surfaced.**
   Routes run both analyzers independently and serialize both accepted collections.
   An observation sequence can therefore be reported as both a pass and a shot,
   with no overlap diagnostic or shared evidence identifier. This is observable
   ambiguity, not a request to classify events; overlap detection should be
   diagnostic-only until a later classification phase.

2. **Single-step speed evidence is vulnerable to tracking jumps and ball identity
   switches.** Both release detectors compute speed from a displacement over one
   frame. A discontinuous reconstructed point can satisfy release-speed and long
   trajectory conditions even though the physical ball did not accelerate that way.
   Trajectory validation checks frame gaps, but not implausible coordinate jumps or
   consistency between individual segment speeds and the overall path. The existing
   thresholds and acceptance decisions should remain untouched; retain a jump flag
   and speed-evidence diagnostics first.

3. **Receiver identity is ambiguous in ties and crowded endings.** Pass receiver
   lookup considers only the final trajectory frame, then selects the maximum of a
   quality/track-id tuple. Equal quality therefore resolves by track ID rather than
   an explicit identity-confidence rule. It does not retain competing receivers or
   distinguish a newly visible track from a stable receiver track.

4. **Configuration ownership differs across otherwise similar evidence.** Pass
   detection reads thresholds from `Settings`, while shot detection has a separate
   hard-coded `ShotDetectionConfig` default. This prevents a single recorded
   configuration profile from fully reproducing pass and shot results. It also
   increases the risk that shared evidence diverges due to independently maintained
   defaults.

5. **Invalid-FPS accounting is inconsistent.** `PassDetector.analyze()` returns
   `raw_pass_candidates == 0` and one rejected candidate for non-positive FPS,
   making `raw != accepted + rejected`. `ShotDetector.analyze()` has no equivalent
   direct-input guard. API metadata validation normally protects this path, but unit
   callers and future integrations can encounter inconsistent diagnostics.

6. **Trajectory windows can incorporate later, unrelated ball activity.** Each
   analyzer scans a fixed future window after release rather than ending trajectory
   evidence at a detected possession transition. A later control event can be
   included in the candidate path. This is especially significant when an event
   candidate begins close to another possession.

### Low

1. **Receiver lookup complexity is linear in the number of tracked players per
   pass candidate.** The current scan is appropriate for the expected small player
   count, but its complexity and tie behavior should be documented if longer clips
   or many candidate possessions become supported.

2. **Preparation and follow-through have limited evidence provenance.** Shot
   preparation is derived from possession-window displacement and follow-through
   from post-release player-box continuity. Their returned confidences do not retain
   their contributing frame-level measurements, limiting debugging without changing
   the present formulas.

3. **Visualization contains accepted events only.** The debug renderer can draw
   accepted pass and shot paths, but it cannot visualize rejected partial evidence,
   overlap, receiver alternatives, or jump warnings because those diagnostics are
   not retained.

## Candidate accounting assessment

For ordinary positive-FPS input, each possession is either accepted or assigned one
aggregate rejection reason in both analyzers. The aggregate counts generally
reconcile. The non-positive-FPS pass early return is the exception described above.
Neither analyzer preserves individual rejected records, so the reconciliation cannot
be independently audited from the response.

## Shared-evidence decision

The duplicated possession/release/trajectory computations demonstrate a real shared
concept, but the current public event records intentionally differ: pass adds
receiver evidence and shot adds preparation, follow-through, speed statistics, and
release acceleration. No shared event base class or combined event engine is
appropriate.

The smallest future extraction is:

```text
possession observations → ReleaseEvidence → TrajectoryEvidence
                                  ├─ receiver evidence → pass decision
                                  └─ preparation/follow-through → shot decision
```

Before extracting it, add fixed-input parity tests that assert each analyzer's
accepted IDs, frame ranges, quality values, and rejection breakdowns are unchanged.
This review deliberately leaves the duplicated code intact to satisfy the required
outcome-preservation constraint.

## Test assessment

Existing deterministic integration tests cover short and long successful events,
missing receivers/releases, fragmented trajectories, multiple players, and power
shots without model inference. Gaps include:

- a simultaneous pass/shot overlap diagnostic case;
- coordinate-jump / ball-identity-switch evidence;
- receiver-quality ties and multiple equally plausible receivers;
- invalid or zero FPS direct-analyzer calls;
- individual rejected-candidate retention and visualization;
- shared-evidence parity before a future extraction.

## Changes made

No production or test code was changed. This report is the only added file.

## Remaining risks

The current event behavior remains stable, but false positives caused by reconstructed
ball jumps, ambiguous receivers, and pass/shot overlap cannot be explained from the
returned diagnostics. The duplicated evidence paths also remain a future divergence
risk until characterized and extracted without changing acceptance behavior.
