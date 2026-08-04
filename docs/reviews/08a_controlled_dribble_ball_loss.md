# Controlled movement, dribble, and ball-loss review — Phase 8A

Scope: candidate generation, feature extraction, acceptance/rejection, confidence,
diagnostics, and shared geometry for controlled movement, dribble, and ball loss. Pass and
shot were excluded. No thresholds or outcomes were changed.

## Changes applied

None. No extraction was safe without changing diagnostic or candidate behavior. The three
pipelines share small geometric primitives but have materially different evidence lifecycles.

## Findings

### Critical

None found.

### High

#### Candidate-limit truncation breaks raw/accepted/rejected reconciliation

Controlled movement, dribble, and ball-loss methods slice accepted candidates to
`technical_event_max_returned_events` before returning them. Their raw counts retain all
evaluated inputs, while rejection counters do not include accepted candidates omitted by the
return limit. If the limit is exceeded, `raw != accepted + rejected`. Unlike interaction
analysis, `TechnicalEventAnalyzer._result` validates confidence bounds only and does not
enforce accounting reconciliation.

### Medium

#### Rejected candidate retention is uneven

Controlled movement retains one statistics dictionary per evaluated interaction, including an
`accepted` flag and one `rejection_reason`. Dribble retains richer statistics and a joined
comma-separated `rejection_reasons` string. Ball loss retains only aggregate counters; it
discards per-segment features/frame range/rejection reason. This prevents debugging false
negative ball-loss decisions at the same fidelity as controlled/dribble candidates.

#### Controlled and dribble candidate IDs are coupled to source IDs

Controlled IDs are `controlled-{interaction_segment_id}` and dribble IDs are based on
controlled IDs. This is deterministic but makes IDs depend on interaction acceptance/order and
not an independent event-evaluation sequence. Ball-loss IDs also derive from interaction IDs.

#### Shared geometry is duplicated but should not be globally merged yet

`_position`, `_distance`, `_clamp`, and `_cosine` are correctly shared within
`TechnicalEventAnalyzer`. Similar distance/bottom-center helpers exist elsewhere, but their
normalization/semantics differ. Controlled evaluates interaction segments; dribble evaluates
accepted controlled events plus movement paths; ball loss evaluates post-interaction evidence.
A shared generic event engine would hide those meaningful differences.

#### Confidence and rejection semantics vary across pipelines

Controlled has a single ordered rejection reason and separates confidence rejections from
other rejections. Dribble accumulates multiple reasons and categorizes a candidate as
low-movement unless its only reason is confidence. Ball loss only counts missing post-evidence
and recovery rejections; failures of separation/speed/confidence are not counted individually.
This is an inconsistent diagnostic contract, though not necessarily a calculation error.

### Low

#### Numerical and frame safety

- Positive FPS is checked before analysis and duplicate player/ball frame indexes are rejected.
- Controlled and ball-loss normalization use a height guard; dribble uses `height if height else
  0.0` guards. `_cosine` rejects short vectors before division and clamps its result.
- Candidate confidences are clamped and final accepted confidences are bounded by `_result`.
- Several statistics fields are untyped dictionaries, so units and optional values are not
  structurally enforced.

#### Complexity/data scans

Controlled scans each interaction frame range; dribble scans movement trajectory for every
accepted controlled candidate; ball loss scans post-event windows for every interaction. These
can overlap substantially but are linear per event. No clear all-pairs search was observed.
An index of movement points by frame could reduce repeat scans if profiling identifies this as
a hot path; it should not be introduced preemptively.

#### Logging

Controlled logging records evaluated segment features and rejection reasons at warning level.
Dribble/ball loss have less equivalent structured per-candidate logging. Logging every
controlled candidate at warning level may be noisy for normal production runs.

## Exact modified files

- `docs/reviews/08a_controlled_dribble_ball_loss.md`

## Remaining risks

1. Returned candidate counts can fail to reconcile when the max-return cap is reached.
2. Ball-loss rejected-candidate evidence is insufficient for visual/debug diagnosis.
3. Rejection taxonomy differs by event type, complicating aggregate analytics.
4. Repeated range scans may become expensive with many interaction segments.
