# ADR-005: Dominant visual target for the MVP

## Status

**Proposed — awaiting Apex contract confirmation**

Date: 2026-08-29. Decision owners: Super-7 product-semantics owner and Apex contract owner.

## Context and problem

`playerId` is Apex business identity, while ByteTrack `track_id` is temporary and analysis-local.
Current `src/services/segment_selection.py:select_segment` selects the highest composite-quality
segment; it neither binds that segment to `playerId` nor compares it with a runner-up for dominance.
Ratings may therefore be mathematically valid for a visually selected but unintended person.

## Decision

Apex provides a product/workflow guarantee that each submitted video is captured or uploaded to
analyse the associated `playerId`; other players may still appear. This is not visual identification
and does not establish `playerId == track_id`.

For the current MVP, the proposed selection method is `dominant_visual_candidate`: select from
qualifying visual candidates primarily by supported visible duration, using existing qualification
signals (visible frames, supported continuous duration, mean detection confidence, current
segment-quality requirements, and normalized-jump protection). Continuity is analyzability evidence
only, never identity maintenance. Qualification and dominance are separate: one qualifying candidate
may coexist with a plausible alternative that falls below normal rating-analysis qualification. A
candidate is `ESTABLISHED` only when it qualifies and no plausible alternative remains unresolved
enough to make visual selection ambiguous. Exact operational policy and thresholds are unresolved.

## Definitions and semantics

- **Business identity:** Apex `playerId`.
- **Visual target:** provisional person selected for this dedicated request.
- **Tracking identity:** temporary `track_id` within one analysis.
- `target_selection_status`: `ESTABLISHED` or `NOT_ESTABLISHED`.
- Future `identity_continuity_status`: `MAINTAINED`, `UNCERTAIN`, `LOST`, or `NOT_EVALUATED`.

`ESTABLISHED` means an acceptable visual target was provisionally established for the dedicated
request under this contract. It is not biometric/jersey/real-world/persistent/cross-video identity
verification, and **Target established does not mean target maintained**. Sprint 1 does not
implement continuity; if conceptualized, it is `NOT_EVALUATED`.

No qualifying candidate uses `no_qualifying_visual_target`; ambiguous plausible candidates use
`ambiguous_visual_target`; other establishment failure uses `target_not_established`.

## Rating, Overall, event, and presentation consequences

When target is `NOT_ESTABLISHED`, player-attributed ratings, detailed ratings, Overall, and Overall
confidence are null/unavailable. This is insufficient target evidence, not analysis failure or zero.
Candidate/event observations remain internal diagnostics; no new public unattributed-event surface
is approved.

Proposed Overall availability is target established, Technical available, and at least one of
Physical Activity or Ball Involvement available. Game Intelligence remains excluded. Overall must
consume explicit availability decisions, not non-null Python values alone; numeric weights/formula
are not changed here.

Event candidate/acceptance/confidence is separate from skill evidence/eligibility/rating. A detected
shot with confidence 0.84 may still have null shooting skill. Public Game Intelligence is proposed
null because required tactical context is absent; any retained heuristic is internal only. Physical
Activity remains an image-space visual movement/activity indicator for an established target, not
fitness, stamina, distance, real-world speed, physical ability, or physiology.

## Diagnostics and compatibility

Minimum conceptual diagnostics: target status, method, `identity_verified=false`, candidate count,
selected and runner-up supported visible duration, reason code, and qualification-evidence summary.
`track_id` remains internal/diagnostic unless Apex establishes a concrete need. Public fields and
callback shape require Apex agreement; this ADR changes no API/schema.

## Alternatives and non-goals

Rejected/deferred for Sprint 1: always choose first/highest candidate (small lead is ambiguous);
manual point/bounding-box seed (preferred later fallback); first-seconds handshake; coloured/ArUco
marker; jersey OCR; appearance Re-ID pending measured failures; SoccerNet GSR; and rejecting every
clip until manual selection (not useful MVP behavior). No continuity, Re-ID, tracklet recovery,
tracker redesign, manual UI, pitch calibration, or new infrastructure is included.

## Risks, validation, and revisit

A risk to prevent is unsafe single-qualifier acceptance: one qualifying candidate alone must not
establish a target without alternative-plausibility/dominance assessment. Accepted risks: selected
dominant player can still be unintended; users can violate the input
guarantee; tracking can fragment/switch; cuts/occlusions are unsupported; thresholds are unvalidated;
correct clips may be rejected and ambiguous clips may pass; ratings are uncalibrated; Physical is
image-space; Game Intelligence is publicly unsupported.

Future validation (Proposed): wrong-player analysis, unsafe acceptance, correct rejection,
eligible-clip, ambiguous-selection, no-qualifying-target rates, supported-analysis coverage per
clip, and aggregate coverage. `supported_analysis_coverage = identity-supported analyzed duration /
total candidate analysis duration`. Continuity is not implemented, so Sprint 1 can only measure
selected-segment coverage, not fully validate identity coverage. Safety has priority, but rejecting
everything is not useful and coverage must never rise through unsafe identity assumptions.

Revisit for unacceptable wrong-player/ambiguous rates, low eligible coverage, ID switches, cuts,
multiplayer/verified-identity/full-match/real-world-metric requirements, or a committed tactical
feature. Rollout requires Apex confirmation, dominance-policy discovery, threshold/validation
evidence, contract tests and a compatibility plan. Rollback removes the future gate under the approved release plan; it does not
create persistence or change existing formula weights.

## Evidence

Discovery: `docs/workplans/sprint-1/02-target-and-identity-discovery.md`,
`03-rating-semantics-discovery.md`, `04-contract-decisions-required.md`, and
`05-minimal-implementation-map.md`. Prior semantics: `ADR-001`, `ADR-002`, and `ADR-003`.
