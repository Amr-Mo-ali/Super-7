> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Sprint 1 discovery: repository baseline

## Executive summary

The inspected runtime selects an analyzable visual segment, not a verified Apex player. Sprint 1
therefore remains blocked pending human and Apex decisions D1–D14: it must provide minimum initial
target safety and truthful rating semantics without silently making all ratings unavailable or
claiming real-world identity/continuity. The runtime baseline is `7920375…`; `f2d1e834…` is the
later documentation-only commit. This correction working tree modifies only this discovery pack.

## Purpose and scope

This evidence pack records the current Super-7 request lifecycle, target/identity semantics,
and rating behavior for the Sprint 1 objective: **prevent wrong-player and misleading ratings**.
It is discovery and proposal work only: no runtime, public contract, formula, configuration, or
deployment change is authorized by it. The original task prohibited commit/push; after discovery,
the documentation was nevertheless committed and pushed. This correction records that fact and
does not rewrite history.

Inspection date: 2026-08-27. Branch: `the-new-inhancement`. **Inspected runtime baseline**:
`7920375b915e852486643df8eb5bf27bf8fb09ae`; its working tree was clean (`git status --short`
returned no entries). **Documentation commit**:
`f2d1e834843bbdc542cc36bdbf05ef7f127fd617`. Git diff confirms the commit range contains only the
eight Markdown files in this directory, so no runtime behavior changed between the inspected
baseline and documentation commit. The requested `docs/vision/` directory is absent.

The current working tree is a documentation-correction tree based on that documentation commit.

No Sprint 1 runtime implementation may begin until the discovery evidence is
reviewed and the Target Eligibility, Rating Availability, Overall Availability,
Event-versus-Skill, and Apex compatibility decisions are explicitly approved.

## Reading order and progress

1. [Discovery log](00-discovery-log.md) — completed command/evidence journal.
2. [Current behavior](01-current-behavior.md) — completed lifecycle baseline.
3. [Target and identity](02-target-and-identity-discovery.md) — completed discovery.
4. [Rating semantics](03-rating-semantics-discovery.md) — completed discovery.
5. [Contract decisions](04-contract-decisions-required.md) — review required.
6. [Minimal implementation map](05-minimal-implementation-map.md) — proposal only.
7. [Verification results](06-verification-results.md) — completed documentation checks.
8. [Dominant target implementation discovery](07-dominant-target-implementation-discovery.md) — proposed minimal design; implementation remains blocked.

Task status: additive `CallbackPayload` availability schema and completed-result mapping are
implemented and tested. `CompletedResponse` now strictly carries both legacy/explicit available
and explicit successful-unavailable states, including parent/child serialization and unavailable
callback projection. Resolver integration, target-unavailable pipeline emission, and deployment
remain pending. A route-level resolver-integration contract is red and awaiting separate human
review; Public Rating V2 redesign is not required.

## Evidence language

Every material claim uses one of these exact classifications: **Implemented and
production-wired**, **Implemented but not production-wired**, **Documented decision**,
**Empirically observed**, **Proposed**, or **Unknown / requires verification**. Sources are
ranked: production code, tests, ADRs/contracts, runbooks, handoff/README, future/backlog,
then chat assumptions. Documentation never substitutes for executable evidence.

## Existing authoritative context

- [Handoff index](../../handoff/README.md), [scoring semantics](../../handoff/scoring-and-product-semantics.md), and [runtime](../../handoff/system-and-runtime.md)
- [ADR-001](../../decisions/ADR-001-video-scoring-semantics.md), [ADR-002](../../decisions/ADR-002-overall-rating-current-state.md), [ADR-003](../../decisions/ADR-003-null-and-evidence-policy.md)
- [ADR-005: dominant visual target](../../decisions/ADR-005-dominant-visual-target-mvp.md) and [target-selection contract v1](../../contracts/target-selection-contract-v1.md) — **Proposed, awaiting Apex contract confirmation**
- [Job contract](../../contracts/analysis-job-contract-v1.md), [process-pool runbook](../../runbooks/process-pool-mvp-benchmark.md), and [concurrency baseline](../../runbooks/concurrency-baseline.md)

## Blockers

Approval is required for target establishment semantics, representation of unavailable ratings
and Overall, event-confidence wording, and callback/API compatibility with Apex. There is no
implemented visual proof that Apex `playerId` belongs to a selected temporary `track_id`.

Target establishment and continuity must remain separate proposed concepts:
`target_selection_status` is `ESTABLISHED` or `NOT_ESTABLISHED`; future
`identity_continuity_status` is `MAINTAINED`, `UNCERTAIN`, `LOST`, or `NOT_EVALUATED`. Neither
status is implemented. **Target established does not mean target maintained.** Establishment would
not, by itself, prove real-world identity. Sprint 1 is limited to minimum initial target safety and
truthful rating semantics after contract approval; continuity remains `NOT_EVALUATED` and no Re-ID,
tracklet recovery, or tracker redesign is in scope.

The following is a **Proposed** conceptual hierarchy, not a current late pipeline phase:

```text
Request and input guarantee
→ Detection and tracking
→ Build `PlayerTrack` visual candidates
→ Visual-target establishment eligibility and dominant track
→ Selected target-track segment
→ Evidence
→ Per-rating eligibility
→ Public response
```

Eligibility is not one late pipeline phase. Overall must consume explicit rating-availability
decisions, not only Python non-null values.

## Current decision status

Conceptually approved: dedicated-video input association; `dominant_visual_candidate`; ambiguous or
unqualified targets are unavailable; target/continuity separation; target-gated ratings; Technical
plus one additional core category for proposed Overall availability; public Game Intelligence null;
and Physical Activity as image-space visual activity. Apex still must confirm public diagnostics,
callback/API compatibility, schema/versioning, and input-guarantee enforcement. Thresholds and
validation remain blocked. Correct automatic order is Request/input guarantee → Detection/tracking
→ qualifying `PlayerTrack` candidates → establishment/dominant track → selected target-track segment → evidence → rating eligibility
→ public response.

Dominance hotfix: one qualifying candidate is not automatically dominant. Proposed establishment
also requires no unresolved plausible alternative; operational qualification/plausibility/dominance
definitions remain blocked on implementation discovery and validation evidence.
