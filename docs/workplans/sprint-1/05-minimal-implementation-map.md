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

This is **Proposed**, not implemented. Existing selected-track diagnostics may remain available
internally as analyzability evidence, but must not be presented as requested-player proof.

Likely files/symbols: `src/schemas/analysis.py` (internal result/public presentation only after
contract approval), `src/services/selection.py` and/or `segment_selection.py` (minimal eligibility
value), `src/api/routes.py` (pipeline gate), `src/api/public_rating_mapper.py` and
`src/services/detailed_rating/engine.py` (unavailable projection), callback schemas, and focused
selection/rating/route/contract tests. A single frozen eligibility value plus reason code is enough;
do not introduce a policy framework.

Proposed reason codes: `target_identity_not_established`, `ambiguous_visual_target`,
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
deployment changes. Missing approved establishment evidence blocks implementation.
