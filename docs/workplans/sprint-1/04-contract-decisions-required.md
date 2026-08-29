> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Decisions required before Sprint 1 implementation

This retains D1–D10 from the original discovery pack and adds review-required decisions. No
threshold is selected here. `target_selection_status` and `identity_continuity_status` are
**Proposed** separate concepts; target establishment would not verify real-world identity.

| ID | Decision required | Current behavior | Available options | Recommended minimum | Compatibility impact | Owner | Blocker | Evidence/source |
|---|---|---|---|---|---|---|---|---|
| D1 | Define initial target establishment. | Selector establishes visual analyzability only. | Never auto-establish; establish only under approved restricted evidence; add an Apex/manual visual seed later. | Define an explicit conservative contract before a numeric rating gate. | New status/reason requires callback/API agreement. | Product + Apex | yes | `src/services/segment_selection.py:select_segment`; `02-target-and-identity-discovery.md` |
| D2 | Can legacy automatic selection establish target? | No code binds `playerId` to track/segment. | Always `NOT_ESTABLISHED`; allowed only for an approved restricted class; defer all legacy use until manual selection exists. | Do not treat a legacy selected segment as proof by default. | May make current ratings unavailable. | Product + Apex | yes | `src/schemas/analysis.py:AnalyzeRequest`; `segment_selection.py`; `tests/test_segment_selection.py` |
| D3 | Represent ambiguity. | Segment ranking has no second-candidate margin gate. | Explicit unavailable reason; retain current selection; add a future manual flow. | Status/reason; never silently emit player ratings for ambiguity. | Additive/versioned callback change may be needed. | Super-7 + Apex | yes | `src/services/segment_selection.py:rank_segments`; `tests/test_selection.py` |
| D4 | Represent establishment and maintenance. | Neither public state exists; segment continuity is a ranking input. | Internal-only initially; additive public states; defer public continuity entirely. | Separate `target_selection_status` from future `identity_continuity_status`; initially no maintenance claim. | API/callback shape decision. | Apex coordination | yes | `src/services/segment_selection.py:TrackSegment.continuity_ratio` |
| D5 | Represent rating availability/reasons. | V2 has status/reason; callback detailed ratings are nullable only. | Preserve current nulls; add callback reason/status; version a richer surface. | Preserve null and agree reason representation before exposing eligibility failure. | Consumers may distinguish missing/null fields differently. | Apex coordination | yes | `src/schemas/public_rating_v2.py`; `src/services/callback_service.py:DetailedRatings` |
| D6 | Overall availability/confidence. | Two categories can produce numeric Overall; no target gate. | Require target establishment only; also tighten coverage; preserve current formula. | If target is not established, all player ratings/Overall and Overall confidence unavailable; do not change formula incidentally. | Semantics affect existing product displays. | Product + Apex | yes | `src/services/player_rating/engine.py:_overall`; ADR-002 |
| D7 | Minimum rating coverage/confidence. | Existing two-category and duration/coverage confidence behavior. | Retain; require all three; set an approved coverage/confidence gate. | Decide separately after eligibility; do not select a threshold here. | Formula/meaning review. | Product | yes | `src/services/player_rating/engine.py`; `tests/test_player_rating_engine.py` |
| D8 | Game Intelligence and Physical naming. | Both are public provisional proxies. | Retain with limitation; rename/version; return unavailable. | No silent reinterpretation; use approved wording/version. | Public semantic compatibility. | Product + Apex | yes | `game_intelligence.py`; `physical_activity.py`; ADR-001 |
| D9 | Event confidence versus skill fields. | Pass/shot detailed numbers aggregate event confidence. | Retain with explicit evidence wording; rename/version; return unavailable. | Do not describe event confidence as skill, completion or finishing. | Callback contract/product UI. | Product + Apex | yes | `src/services/detailed_rating/engine.py:_event_score`; `tests/test_detailed_rating.py` |
| D10 | API versioning. | Existing acceptance/callback consumers may depend on shape and semantics. | Additive fields; versioned callback; coordinated breaking version. | Prefer additive/versioned compatibility plan. | Apex migration may be required. | Apex coordination | yes | `src/schemas/analysis.py`; `callback_service.py`; contract v1 |
| D11 | Apex input guarantee: does workflow guarantee video visibly contains requested player, and when? | Repository evidence supplies only request identifiers; no such guarantee is encoded. | No guarantee; documented workflow guarantee; future visual seed/attestation. | Treat as unknown until Apex/product supplies an approved guarantee. | Defines whether a restricted eligibility contract is possible. | Apex + product | yes | `AnalyzeRequest`; no visual binding found; **Unknown / requires verification** |
| D12 | Single-player safe-auto eligibility: can an Apex-associated, one-dominant-candidate clip be established? | Segment selector ranks eligible segments; it does not test one-candidate dominance or identity. | Never auto-establish; allow an approved restricted single-player contract; wait for manual seed. | Only consider after D11 and an explicit D1/D2 evidence contract; do not select thresholds here. | May preserve some ratings or make them unavailable. | Product + Apex | yes | `segment_selection.py:rank_segments,select_segment`; `tests/test_segment_selection.py` |
| D13 | Multiplayer behavior until manual selection. | A segment may be selected despite no identity/dominance evidence. | Return unavailable for all multiplayer/ambiguous clips; retain legacy ratings; permit only approved restricted single-player clips. | Do not emit player ratings for ambiguous/multiplayer clips absent an approved establishment contract. | Availability change for existing callbacks. | Product + Apex | yes | `segment_selection.py`; `tests/test_selection.py` |
| D14 | May unattributed video/event observations survive establishment failure? | Pipeline currently attributes downstream evidence to selected track. | Return nothing; return clearly unattributed observations; retain internal diagnostics only. | Decide a separately labelled, non-player-attributed surface; all player ratings unavailable. | New fields or semantic segregation. | Product + Apex | yes | `src/api/routes.py:_completed`; `public_rating_mapper.py` |
| D15 | Record documentation/process deviation. | Original brief said no commit/push; documentation was later committed as `f2d1e834…` and, per correction request, pushed. | Retain history and document; rewrite history (not recommended). | Retain commit/history; no rewrite. Local Git confirms the commit and its file-only diff; push is recorded from supplied correction instruction, not independently verified locally. | Process/audit record only; no runtime impact. | Documentation owner | no | `git diff --name-status 7920375… f2d1e834…`; continuation request |

Implementation remains blocked by D1, D2, D11, D12 and D13. There is no repository evidence that
Apex guarantees the requested player is visible in the submitted clip.

## Contract-approval addendum — documented decision status

The entries above preserve discovery history. The following reflects the approved product semantics
from the Sprint 1 contract brief; API/schema compatibility and operational thresholds remain pending
Apex/implementation confirmation.

| ID | Product semantic status | API/threshold status | Current conclusion |
|---|---|---|---|
| D1 | Approved conceptually: restricted automatic establishment is allowed under the dedicated-video guarantee. | Public representation pending Apex; threshold pending validation. | `dominant_visual_candidate`, not identity verification. |
| D2 | Approved: current legacy selected segment is not automatically established. | Future implementation must apply the contract. | Existing selector is insufficient alone. |
| D3 | Approved: ambiguity yields `NOT_ESTABLISHED`. | Reason/public shape pending Apex. | `ambiguous_visual_target`. |
| D4 | Approved: establishment and continuity are separate. | No continuity state implementation in Sprint 1. | Continuity conceptually `NOT_EVALUATED`. |
| D6 | Approved: target failure makes ratings/Overall unavailable. | Callback/API change pending Apex. | Technical + one other core category proposed for Overall. |
| D8-A | Approved: public Game Intelligence unavailable/null for MVP. | Existing public field behavior cannot change without Apex agreement. | Internal heuristic only under non-overclaiming diagnostics if retained. |
| D8-B | Approved: Physical Activity means image-space visual movement/activity. | Final public name/schema pending Apex. | It may remain available for established target if existing gate passes. |
| D11 | Approved product guarantee: request video is dedicated to `playerId`, but other players may appear. | Enforcement/attestation remains an Apex workflow question. | It never proves `playerId == track_id`. |
| D12 | Approved concept: restricted dominant-candidate auto-selection. | Dominance definition/thresholds unresolved. | Primary evidence is supported visible duration. |
| D13 | Approved: ambiguous/multiplayer without clear dominance returns unavailable ratings. | Public reason/status pending Apex. | No silent first-place acceptance. |
| D14 | Approved: unattributed observations remain internal in Sprint 1. | No public unattributed-event surface. | Player ratings remain unavailable. |
| D15 | Retain documentation history; no rewrite. | No API impact. | Documentation commit is retained. |

D5, D7, D9 and D10 remain API/product compatibility decisions awaiting Apex confirmation; no
schema, callback, formula, or threshold has been approved for implementation here.

### Dominance-policy hotfix — Documented decision / Proposed contract behavior

Two future decisions are required: (1) top-candidate qualification policy and (2)
plausible-alternative/dominance policy. Rating-analysis qualification must not be reused as the sole
ambiguity boundary without evidence: a slightly sub-threshold visual candidate can still be a
plausible alternative. Future selector discovery must inspect all candidate information, including
candidates rejected by rating-analysis qualification. No new framework or numeric threshold is
proposed.
