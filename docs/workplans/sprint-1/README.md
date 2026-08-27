> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Sprint 1 discovery: repository baseline

## Purpose and scope

This evidence pack records the current Super-7 request lifecycle, target/identity semantics,
and rating behavior for the Sprint 1 objective: **prevent wrong-player and misleading ratings**.
It is discovery and proposal work only: no runtime, public contract, formula, configuration,
deployment, commit, or push change is authorized by it.

Inspection date: 2026-08-27. Branch: `the-new-inhancement`. Commit:
`7920375b915e852486643df8eb5bf27bf8fb09ae`. Start working tree: clean (`git status --short`
returned no entries). The requested `docs/vision/` directory is absent.

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

Task status: discovery complete; approval is the next permitted step. Runtime implementation is
not authorized.

## Evidence language

Every material claim uses one of these exact classifications: **Implemented and
production-wired**, **Implemented but not production-wired**, **Documented decision**,
**Empirically observed**, **Proposed**, or **Unknown / requires verification**. Sources are
ranked: production code, tests, ADRs/contracts, runbooks, handoff/README, future/backlog,
then chat assumptions. Documentation never substitutes for executable evidence.

## Existing authoritative context

- [Handoff index](../../handoff/README.md), [scoring semantics](../../handoff/scoring-and-product-semantics.md), and [runtime](../../handoff/system-and-runtime.md)
- [ADR-001](../../decisions/ADR-001-video-scoring-semantics.md), [ADR-002](../../decisions/ADR-002-overall-rating-current-state.md), [ADR-003](../../decisions/ADR-003-null-and-evidence-policy.md)
- [Job contract](../../contracts/analysis-job-contract-v1.md), [process-pool runbook](../../runbooks/process-pool-mvp-benchmark.md), and [concurrency baseline](../../runbooks/concurrency-baseline.md)

## Blockers

Approval is required for target establishment semantics, representation of unavailable ratings
and Overall, event-confidence wording, and callback/API compatibility with Apex. There is no
implemented visual proof that Apex `playerId` belongs to a selected temporary `track_id`.
