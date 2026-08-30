> Status: Discovery evidence and proposed design — not an approved implementation contract.
>
> Inspected commit: `275141d7ed57215352c47a3e5d5c14a7d20fd89d`. No runtime behavior changed.

# Dominant visual target implementation discovery

## Corrected executive conclusion

The proposed target-selection and dominance candidate unit is **`PlayerTrack`**, not
`TrackSegment`. `PlayerTrack` represents the tracker-returned observations for one temporary,
analysis-local `track_id`; it is therefore the appropriate unit for comparing observable visual
candidates. A `TrackSegment` is the **selected evidence/analysis unit inside the winning track**.

```text
TrackingRun → PlayerTrack summaries → dominant-track selection → selected track_id
→ qualifying segments belonging only to selected track_id → best supported segment
→ evidence and ratings
```

The current implementation is **Implemented and production-wired** but segment-first: it ranks
every eligible `TrackSegment` by composite quality and selects the first. That can make two
fragments of one `track_id` compete as identity alternatives and let a shorter segment from track B
defeat a more observable track A. Distinct `track_id` values remain separate alternatives; no
continuity or Re-ID claim is introduced.

## Source-verified tracking evidence

[`PlayerTrack`](../../../src/services/selection.py) has `track_id`, `visible_frames`,
`total_frames`, `longest_segment`, `lost_track_count`, `average_confidence`, ball fields, and
`visibility_ratio` (`visible_frames / total_frames`, or `0.0` when total frames is zero).
[`DetectionOnlyPlayerTracker._summary`](../../../src/services/player_tracker.py) constructs it from
actual tracker-returned observations: `visible_frames = len(values)`, `total_frames = processed`
decoded frames, `average_confidence` is the mean observation confidence, and the longest run counts
only adjacent frame numbers. `lost_track_count = len(sorted_frames) - longest`.

The tracker appends every returned `(frame_index, confidence)` to its per-track list, but its boxes
and confidence dictionaries overwrite a repeated frame key. Thus `PlayerTrack.visible_frames` can
count duplicate same-track/frame outputs whereas segment construction deduplicates keys. This is
source behavior, not evidence that duplicates occur in production. The tests-only policy therefore
defines the track candidate from unique `TrackingRun.player_boxes[track_id]` frame keys: unique
visible count, unique-count/processed-frame visibility ratio, and unique-count/FPS visible duration.
It must safely reject non-positive/non-finite processed-frame or FPS metadata; it does not use
`PlayerTrack.visible_frames` blindly.

`_analyze_uploaded` has `metadata.fps` and passes it to
[`build_segments`](../../../src/services/segment_selection.py). The proposed observable-track duration
is unique visible frames / `metadata.fps`, subject to the explicit invalid/zero-FPS rule. It measures
returned unique frame support, not inclusive span; no threshold is selected here.

`TrackSegment` stores `track_id`, frame bounds, duration, unique visible frames, continuity,
confidence, geometry/stability/ball terms, composite quality, and rejection reasons.
`build_segments` splits one track's sorted box-frame keys on a large missing-frame gap or normalized
center jump. Its duration is inclusive span/FPS (or `0.0` for falsy FPS), so it may include
tolerated short gaps. It remains the evidence unit after track selection, not the dominance unit.

## Legacy selector and current default composition

[`TargetPlayerSelector`](../../../src/services/selection.py) exposes `rank` and `select` over
`PlayerTrack`. `WeightedTargetPlayerSelector.rank` filters tracks by visibility ratio, longest run,
and mean confidence, then stable-sorts descending by score. Its score is only visibility ratio:
the method name `visibility_and_track_continuity` does not weight continuity or confidence. Default
profile values are margin `0.08`, minimum visibility `0.20`, continuous frames `5`, and confidence
`0.50` ([profile](../../../src/config/football_profiles.py)).

Its `select` rejects only when top-two difference is strictly less than `selection_margin`. A tie
is rejected for a positive margin; an exact positive boundary is accepted. With zero margin, an
exact tie is not rejected (`0 < 0` is false), so stable sorting makes the winner input-order
dependent. Existing tests cover most-visible selection, no ball dependency, unavailable ball
tracking, an ambiguous `0.1` tie, below-visibility rejection, and no candidate
([`test_selection.py`](../../../tests/test_selection.py)).

Composition injects that selector, but default `Settings.target_selection_mode` is `"segment"`.
In segment mode [`_analyze_uploaded`](../../../src/api/routes.py) builds/ranks/selects all segments
and sets `ranked = (selected,)`; its later generic ambiguity check cannot execute because that
tuple has at most one item. `rank_segments` filters rejection reasons and stable-sorts only
`segment_quality`.

## Corrected candidate semantics and minimal design

| Decision | Concept |
|---|---|
| Track qualification | Is this `PlayerTrack` sufficiently observable to be a target candidate? |
| Track dominance | Is the top qualifying track unambiguous against all plausible other tracks? |
| Segment qualification | Does a continuous portion **within the selected track** support analysis? |

| Option | Consequence | Recommendation |
|---|---|---|
| A. Reuse `WeightedTargetPlayerSelector.select`, then choose a segment | Avoids duplicate tracking but discards near-threshold plausible alternatives, preserves legacy quirks, and may select a track without a qualifying segment. | Do not use directly. |
| B. Extract small pure track qualification/dominance helpers, then rank winning-track segments | Reuses one `TrackingRun`, makes diagnostics/tests explicit, and prevents cross-track or same-track segment competition. | **Recommended.** |
| C. Add a candidate framework | Adds abstraction without demonstrated need. | Reject. |

The recommended flow uses unique-frame track evidence derived from `run.player_boxes` for track
qualification/dominance, retains plausible near-threshold alternatives, then filters existing segments with
`segment.track_id == winning_track.track_id` before `rank_segments`. It does not rerun tracking.
If the winning track has no qualifying segment, target-dependent analysis is unavailable with a
distinct internal reason; another track's segment must not replace it.

Tests provisionally reuse the existing `0.08` selection margin: unique-visibility gap `>= 0.08`
demonstrates dominance, while a smaller gap or exact tie is ambiguous. This is **Proposed** test
policy, not production-wired or validated. Different track IDs are never merged in Sprint 1.
The result must be deterministic and tie-safe: an exact unresolved tie returns `NOT_ESTABLISHED`,
never an incidental input-order winner.

## Isolated implementation status (2026-08-30)

The approved pure module now exists at
[`dominant_target_selection.py`](../../../src/services/dominant_target_selection.py). It implements
only unique-frame evidence, current-settings qualification, plausible-alternative dominance, and
winning-track-only segment selection. The 28 approved unit tests and 42-test existing focused
baseline pass. This is **Implemented but not production-wired**: routes, composition, rating gates,
public mapping, callback behavior, schemas, formulas, and settings remain unchanged.

## Integration-tests-only feasibility stop (2026-08-30)

The requested integration-test step stopped before adding tests. Source inspection confirms that
`api.routes._analyze_uploaded` owns the one existing `tracker.analyze` invocation but remains
segment-first: it does not import or consume `TargetEligibilityResult`, call
`evaluate_dominant_target`, bind an established `track_id` to downstream evidence, or carry an
internal target/availability decision to response projection. `CompletedResponse` requires a
`SelectedPlayer`; `NonCompletedResponse` has no player-attributed ratings; and public
mapper/callback changes require Apex agreement. Existing `PlayerRatingEngine` also preserves the
current formula, which allows Physical Activity plus Ball Involvement Overall when Technical is
unavailable. These facts make a faithful contract for the requested outcomes impossible without a
new production integration seam or a public/formula decision, both excluded from this tests-only
task.

Required human decision: approve the smallest internal child-orchestration result/gate that carries
target status, reason, winning track/segment reference, and rating availability without changing the
public schema. Only then add focused orchestration and projection tests; do not wire production
integration from this discovery record.

## Tests-only internal contract

[`test_dominant_target_selection.py`](../../../tests/test_dominant_target_selection.py) establishes
the following **Proposed** internal-only API without implementing it: `unique_track_evidence` for
unique `player_boxes` keys; `evaluate_dominant_target` for qualification, plausibility, and
unique-visibility-gap dominance; `select_winning_track_segment` for the selected `track_id` only;
and `TargetEligibilityResult` only if a result value is needed. The local test imports deliberately
fail until that module exists, rather than skipping the contract. These names are not public API,
schema, or callback commitments.

## Result boundary and rating gate

A small frozen `TargetEligibilityResult` should hold selected `PlayerTrack` (and optionally its
serializable `track_id` reference), selected `TrackSegment`, track/runner-up evidence, segment
outcome, status, and reason. It should not hold raw observations or non-pickle-safe objects.

The child should own tracking, track dominance, and selected-track segment choice. Existing
child-result transport already carries serialized `response_json`, so no envelope expansion is
needed for an internal gate; the parent remains callback owner. Public diagnostics/status/reason
need Apex API/schema approval.

## Ownership and future orchestration acceptance plan

`dominant_target_selection` owns only target evidence, qualification, dominance, selected-track
segment choice, and `TargetEligibilityResult`. It receives an existing `TrackingRun`; it neither
owns nor executes tracking. A `tracking_runs_consumed` result counter would be artificial product
state. “Tracker called once” belongs in a future orchestration integration test with a mock/spy
around the tracker owner, not in this pure selection unit suite.

The orchestration/analysis pipeline decides whether player-attributed downstream analysis may run.
`PlayerRatingEngine` remains owner of rating and Overall formulas; the public mapper/callback builder
remains owner of response projection. The selection module must not receive arbitrary rating
dictionaries, calculate Overall, null callback fields, own lifecycle state, carry shot-event
confidence, or reproduce rating formulas.

Future integration acceptance criteria (not implemented here):

1. The orchestration pipeline invokes its tracker once per analysis; test near the tracker owner in
   an analysis-route/process orchestration test using a mock/spy.
2. `NOT_ESTABLISHED` keeps analysis completed but prevents player-attributed downstream ratings;
   test in the child analysis-route/orchestration suite.
3. `NOT_ESTABLISHED` projects player-attributed ratings, Overall, and Overall confidence as
   unavailable/null; test in the public mapper/callback integration suite after Apex compatibility
   decisions.
4. `ESTABLISHED` with Technical unavailable keeps Overall unavailable; test beside
   `PlayerRatingEngine` availability integration.
5. `ESTABLISHED` with Technical plus one other core category preserves the existing
   `PlayerRatingEngine` formula; test beside the engine regression suite.
6. Event confidence may remain available while its skill rating is unavailable; test in detailed
   rating/public-event projection integration.
7. Callback/API assertions remain blocked pending Apex compatibility decisions; no public schema is
   implied by these criteria.

## Required tests and boundary

Before implementation, test:

- A has greater total observed visibility than B despite B's longer/better individual segment: A
  wins and only A segments are considered;
- two segments from one track are evidence alternatives, not identity alternatives;
- winning track with no qualifying segment does not substitute another track;
- different track IDs remain separate despite possible real-person fragmentation;
- legacy exact-margin/zero-margin behavior, and future exact tie/reordered-input
  `NOT_ESTABLISHED` behavior;
- invalid/zero FPS and duplicate observations.

Relevant tests include [`test_selection.py`](../../../tests/test_selection.py),
[`test_segment_selection.py`](../../../tests/test_segment_selection.py), and
[`test_player_tracker_isolation.py`](../../../tests/test_player_tracker_isolation.py). Tracker
invocation, rating availability/formulas, serialization, and callback compatibility belong only to
the future integration acceptance plan above. No test was run for this correction.

No numeric qualification, plausibility, dominance, duplicate-observation, FPS, public-diagnostic,
or Apex compatibility policy is selected. Re-ID, continuity recovery, tracker redesign, manual
selection, schema/API/callback changes, settings changes, CV execution, deployment, dependencies,
and formula changes are out of scope. Implementation remains blocked pending human approval.

## Resolver-approved integration-tests-only stop (2026-08-30)

Human review subsequently approved one future `resolve_dominant_target(...)` helper returning only
eligibility and `TrackSegment | None`. That is the correct minimal composition seam: it consumes
the already-returned `TrackingRun`, composes the pure selector operations, and requires neither DI
nor a second tracking run. It permits focused future tests to patch `api.routes.resolve_dominant_target`.

It does **not** authorize or resolve target-unavailable result projection. Source inspection confirms
that `CompletedResponse` requires `SelectedPlayer`, whereas `NonCompletedResponse` has no rating,
Overall, or Overall-confidence fields; the public mapper treats it as a failure-shaped result. The
required successful target-unavailable/null-rating behavior therefore cannot be specified using
existing response fields. The integration-test task stops pending a human-approved representation
decision; no resolver or route wiring was implemented.

## Apex-confirmed response-contract stop (2026-08-30)

The confirmed availability fields and reasons are now recorded in the governing contract and
ADR-005. They establish semantics, but do not choose whether the future compatibility work changes
the callback envelope, Public Rating V2, or their relation. The current callback removes the V2
`player` object and Public Rating V2 lacks the confirmed top-level availability fields. This is an
explicit stop condition for focused schema tests, because asserting either placement would itself
be a public compatibility decision. **Contract confirmed; schema not implemented; mapper not
implemented; pipeline gate not implemented; not deployed.**

## Final CallbackPayload compatibility contract (2026-08-30)

Human review selected `CallbackPayload` as the canonical Apex surface. The future schema/mapping is
additive: preserve all current callback fields, reuse the existing V2 player dictionary, add
`resultAvailability`, `unavailabilityReason`, `player`, and `overallConfidence`, and preserve
`PlayerRatingSummary.overall.confidence` without derivation. Public Rating V2 redesign is not
required. Six red callback contract tests now specify serialization and contradictory-state
invariants; no source implementation, resolver wiring, or pipeline gate was added.

## CallbackPayload green phase (2026-08-30)

The approved additive callback schema and completed-result mapper are now implemented. Available
callbacks preserve existing fields while adding the V2 player dictionary, availability/reason state,
and exact pre-existing Overall confidence. The schema can represent unavailable callbacks and rejects
contradictory unavailable states, but no target resolver, target gate, or unavailable pipeline
emission is wired. This remains **Implemented but not production-wired** for target unavailability.

## Authoritative internal carrier contract (2026-08-30)

`CompletedResponse` is the recommended authoritative internal carrier for a future successful but
target-unavailable result. It is produced by `_analyze_uploaded`; the child serializes it in
`ChildAnalysisSuccess.response_json`; parent validation restores it through `AnalyzeResponse`; and
`_callback_payload` maps it to `CallbackPayload`. `NonCompletedResponse` is not suitable because it
means a separate noncompleted result, and `ParentFailure` is process failure. The new five-test red
contract proves current `CompletedResponse` cannot yet hold null selected-player and score state.
Future green work must alter that carrier or an explicitly approved compatible successful-result
representation, then map it; no resolver, target gate, or pipeline behavior is approved here.
