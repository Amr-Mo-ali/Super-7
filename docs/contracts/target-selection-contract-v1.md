> **Status: Proposed contract. Not implemented and not yet confirmed by Apex.**

# Target-selection contract v1

## Scope, actors, and terminology

This proposed behavioral contract governs MVP automatic visual-target establishment, not an API
shape. Apex associates `playerId` with a dedicated uploaded/captured video; Super-7 detects/tracks
visual candidates and may select one provisionally. `playerId` is business identity, visual target
is the provisional person, and `track_id` is temporary analysis-local tracking identity. The contract
never claims `playerId == track_id`.

## Input guarantee and method

The product guarantee is that the video is intended to analyse the associated player, though other
players may appear. The allowed proposed method is `dominant_visual_candidate`: candidates must
qualify using existing evidence, with supported visible duration primary and current visible-frame,
continuous-duration, detection-confidence, segment-quality and normalized-jump evidence supporting.
Continuity is analyzability only. Numeric dominance definition is deliberately pending.

## States, reasons, and establishment truth table

`target_selection_status` is `ESTABLISHED` or `NOT_ESTABLISHED`. `ESTABLISHED` is provisional
target establishment for the dedicated request—not verified real-world identity or maintenance.
Target established does not mean target maintained. Future continuity is separate and Sprint 1 is
conceptually `NOT_EVALUATED`.

| Dedicated-video guarantee | Qualifying candidates | Clear dominance | Result | Ratings |
|---|---:|---:|---|---|
| absent/unknown | any | any | `NOT_ESTABLISHED` | unavailable |
| present | 0 | no | `NOT_ESTABLISHED` / `no_qualifying_visual_target` | unavailable |
| present | 1 | yes by definition | `ESTABLISHED` | per-rating gates apply |
| present | more than 1 | no | `NOT_ESTABLISHED` / `ambiguous_visual_target` | unavailable |
| present | more than 1 | yes | `ESTABLISHED` | per-rating gates apply |

“Clear dominance” is a contract concept in this task. Its numeric operational definition is not
selected here. Other establishment failure is `target_not_established`.

## Rating and Overall gating

If `NOT_ESTABLISHED`, all player-attributed top-level/detailed ratings, Overall and Overall
confidence are unavailable/null. Null is not zero or analysis failure. If established, normal
per-rating evidence gates still apply. Proposed Overall eligibility requires established target,
Technical available, and at least one additional available core category (Physical Activity or Ball
Involvement); Game Intelligence is excluded. Event confidence is not skill: accepted shot event
confidence may be numeric while shooting skill remains null.

## Diagnostics, public compatibility, and failure semantics

Internal conceptual diagnostics: status, method, `identity_verified=false`, candidate count,
selected/runner-up supported visible duration, reason, and qualification summary. `track_id` stays
internal/diagnostic. Any public fields, callback changes, or versioning remain pending Apex
agreement. Candidate/event observations remain internal when establishment fails; no public
unattributed-event surface is proposed for Sprint 1.

## Examples and limitations

A dedicated clip with one qualifying candidate may be provisionally established; a dedicated clip
with two plausible non-dominant candidates is unavailable. A video not covered by the input
guarantee is unavailable even if one candidate appears strongest. Security/privacy: no biometric,
jersey or persistent identity claim is made; diagnostic exposure must not turn a temporary track ID
into a public identity.

Non-goals: manual selection, continuity/Re-ID, camera-cut recovery, GSR, team/jersey inference,
pitch calibration, public schema change, threshold selection and dataset creation. Unknowns include
dominance policy, API compatibility, guarantee enforcement and validation data. Prerequisites are
Apex confirmation, implementation discovery, contract tests, and validation obligations described
in [ADR-005](../decisions/ADR-005-dominant-visual-target-mvp.md).

## Validation obligations (Proposed)

Future validation must measure wrong-player analysis, unsafe acceptance, correct rejection,
eligible-clip, ambiguous-selection, no-qualifying-target, per-clip supported-analysis coverage, and
aggregate supported-analysis coverage. `supported_analysis_coverage = identity-supported analyzed
duration / total candidate analysis duration`. Sprint 1 lacks continuity, so initial results can
describe selected-segment coverage only, not fully validate identity coverage. Safety is primary,
but rejecting every clip is not useful and coverage must never grow through unsafe assumptions.
