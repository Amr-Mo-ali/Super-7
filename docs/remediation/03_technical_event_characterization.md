# Phase 2.1: technical-event characterization

## Coverage added

`tests/test_technical_events.py` now uses fixed-frame, synthetic domain observations to
characterize controlled-movement, dribble, ball-loss, quality-gate, ordering, identifier,
confidence, duplicate-frame, and candidate-limit behavior.  It contains no video, model,
network, GPU, or timing dependencies.

The controlled-movement suite covers valid movement; minimum duration, displacement,
proximity, and direction thresholds; stationary/fragmented evidence; stable IDs; and
deterministic rejection statistics.  The dribble suite covers directional and progressive
subtypes, turn-angle equality, small-angle and adjacent-turn filtering, turn-rate rejection,
trajectory diagnostics, and confidence bounds.  The ball-loss suite covers accepted loss,
missing evidence, recovery in the window, and evidence after the recovery window.  Quality
tests cover each global quality gate, invalid FPS, empty evidence, and duplicate frames.

## Formulas and decisions protected

The suite asserts the existing controlled confidence expression and bounded dribble
confidence output.  No thresholds, confidence formulas, rejection precedence, event IDs,
diagnostics fields, ordering, or public schemas were changed in this phase.

Threshold equality is acceptance for controlled duration, displacement, proximity, and
direction, and for the dribble turn-angle threshold.  Ball-loss recovery includes the final
frame of the rounded recovery window; later observations are ignored by that gate.

## Accounting observations

The returned-candidate limit is applied after the raw candidates and accepted diagnostics
are counted.  Consequently, when truncation occurs,

`raw != returned accepted + rejected-short + rejected-low-confidence`.

There is no `truncated_accepted_candidates` diagnostic field, so the requested reconciliation
cannot be expressed from the current public contract.  This suite locks down that mismatch
without changing the contract.  Controlled rejection-breakdown totals do reconcile with
controlled rejected counts when no truncation occurs.  Dribble breakdown entries count
reasons, not rejected candidates, so their sum can exceed the number of rejected candidates.

Ball-loss diagnostics only account for missing post evidence and recovery.  Failures of
separation, away-speed, or confidence gates are silently omitted from rejection counts.

## Unresolved algorithm-quality questions

There is no pass-like-release exclusion in the current ball-loss implementation, despite
the desired characterization category.  Also, with the default four-frame turn suppression,
an alternating tracker path can be reduced to a few turns and accepted as a directional
dribble.  The tests document both existing outcomes rather than changing the detector.

## Confirmation

This was a test-and-documentation-only change.  No event behavior was modified.
