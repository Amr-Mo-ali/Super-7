> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Proposed minimal implementation map

Objective: **Prevent Wrong-Player and Misleading Ratings.** The smallest safe proposal is to add
one explicit target-eligibility result at the existing selection/pipeline boundary and make rating
construction obey it:

```text
if target selection is NOT_ESTABLISHED:
    no player ratings
```

This is **Proposed**, not implemented. Current automatic selection establishes analyzability only,
not verified real-world identity or a `playerId` binding. Existing selected-track diagnostics may
remain internally as analyzability evidence, but must not be presented as requested-player proof.
No manual point or bounding-box seed exists in the current request schema or selector flow.
Proposed `target_selection_status` is limited to `ESTABLISHED`/`NOT_ESTABLISHED`; a future separate
`identity_continuity_status` may be `MAINTAINED`, `UNCERTAIN`, `LOST`, or `NOT_EVALUATED`. Sprint 1
does not implement either state or continuity maintenance. **Target established does not mean target
maintained.** After approval, Sprint 1 implements only minimum initial target safety and truthful
rating semantics; continuity remains `NOT_EVALUATED`, with no Re-ID, tracklet recovery, or tracker
redesign.

The intended **Proposed** automatic-flow hierarchy is not one late pipeline phase:

```text
Request and input guarantee
→ Detection and tracking
→ Build qualifying visual candidates
→ Visual-target establishment eligibility
→ Selected target segment
→ Evidence
→ Per-rating eligibility
→ Public response
```

Overall must consume rating-availability decisions from this hierarchy, not merely Python
non-null values.

Smallest future shape: existing tracking output → qualifying candidates → dominant-candidate
eligibility result → rating-availability gate. Likely reusable evidence is `TrackSegment` visible
frames/duration/confidence/continuity, rejection reasons, and normalized-jump splitting in
`src/services/segment_selection.py`. The default segment selector does **not** implement the full
approved contract: `rank_segments()` orders by composite `segment_quality`, `select_segment()` takes
the first candidate, it has no runner-up dominance gate, does not consume the dedicated-video input
guarantee, and emits no establishment status/reason. This is **Implemented and production-wired**
current behavior, verified in `segment_selection.py` and `tests/test_segment_selection.py`. Future
implementation discovery must separately inspect top-candidate qualification and plausible
alternatives, including candidates rejected by rating-analysis qualification; it must not reuse that
rating threshold as the only ambiguity boundary without evidence. No new framework or numeric
threshold is proposed.

Likely files/symbols: `src/schemas/analysis.py` (internal result/public presentation only after
contract approval), `src/services/selection.py` and/or `segment_selection.py` (minimal eligibility
value), `src/api/routes.py` (pipeline gate), `src/api/public_rating_mapper.py` and
`src/services/detailed_rating/engine.py` (unavailable projection), callback schemas, and focused
selection/rating/route/contract tests. A single frozen eligibility value plus reason code is enough;
do not introduce a policy framework.

Proposed reason codes: `target_not_established`, `ambiguous_visual_target`,
`no_qualifying_visual_target`. The exact public spelling/shape requires D1–D10 approval. Data flow
would change from `selected visual segment → ratings` to `selected visual segment → eligibility →
ratings only when established → callback`. Rollback is a controlled reversion of the gate after
contract approval; no storage/data migration is needed because no persistence is introduced.

Required tests: no player identity proof yields unavailable top-level and detailed ratings; numeric
zero remains distinct from unavailable; Overall/confidence unavailable with eligibility failure;
ambiguous/no candidate reasons; legacy callback compatibility; a positive approved establishment
case; and regression tests preserve current formulas for established targets.

PR sequence: (1) approved contract and tests; (2) minimal implementation; (3) API wiring if not
already covered by (2); (4) regression verification. (2) and (3) may combine only if the agreed
surface is trivially small.

Explicit non-goals: identity continuity, Re-ID, jersey OCR, team classification, manual UI, GSR,
pitch calibration/homography, tracker/model redesign, durable queue/database, concurrency or
deployment changes. If legacy automatic selection is always `NOT_ESTABLISHED`, immediately adding
the gate could make all current player ratings unavailable. Implementation is therefore blocked
until D1, D2, D11, D12 and D13 are approved; Sprint 1 must not silently disable all ratings without
a deliberate product and Apex decision. Missing approved establishment evidence blocks
implementation.

## Future validation measures (Proposed)

No validation dataset is created by this task. A future approved validation plan should measure
wrong-player analysis rate, unsafe acceptance rate, correct rejection rate, eligible-clip rate,
supported-analysis coverage per clip, aggregate supported-analysis coverage, ambiguous-selection
rate, and no-qualifying-target rate. Define:

```text
supported_analysis_coverage =
identity-supported analyzed duration /
total candidate analysis duration
```

Safety has priority. A zero wrong-player rate achieved by rejecting everything is not a useful
product; coverage must never be increased by unsafe identity assumptions. Since Sprint 1 does not
evaluate identity continuity, this cannot yet be fully validated as an identity metric; early
measurement can only describe selected-segment coverage. These metrics require labelled/approved
future evidence.
