> Status: Approved discovery evidence; proposed architecture is blocked and not an implementation
> or production-activation approval.

# Sprint 2 durability discovery package

## Baseline and purpose

This package preserves the approved read-only durability discovery performed on 2026-09-13 at
commit `066d31f2283d0e06ffe07a8e1397a98ba4f72b7d`, on branch
`docs/sprint-2-durability-discovery`. At the starting gate, the working tree and index were clean,
the local `origin/main` reference matched the same commit, and divergence was zero in both
directions. No fetch or external-state verification was performed.

Current executable behavior remains the authority. The
[canonical handoff](../../handoff/README.md),
[Sprint 1 verification](../sprint-1/06-verification-results.md), and
[system audit](../system-audit-2026-09-04.md) provide the supporting history. The current service
accepts work into a process-local queue; accepted jobs, lifecycle state, results, cancellation
intent, callback retry state, and artifact retention ordering do not survive all relevant process
or manager recreation boundaries.

## Classification

| Item | Status |
|---|---|
| Current in-memory queue, one worker, one spawned child, and inline callback retries | Current, implemented behavior |
| Restart-safe accepted work, results, delivery, and artifact ownership | Not implemented |
| PostgreSQL-only durable boundary | Proposed; blocked on Apex and infrastructure decisions |
| More workers, Redis, broker, task framework, or Kubernetes | Deferred |
| Production database, migration, deployment, and server validation | Blocked and explicitly outside this package |

The existing [durable-storage ADR](../../decisions/ADR-005-durable-job-storage-and-worker-architecture.md)
records an earlier accepted direction for later implementation. The new
[Sprint 2 ADR](../../decisions/ADR-008-durable-job-recovery-and-callback-outbox.md) preserves that
history while recording the current proposal and its unresolved activation prerequisites. The
historical duplicate ADR-005 filenames are not renamed or rewritten; ADR-008 is the next unused
number in the canonical `docs/decisions` sequence.

## Reading order

1. [ADR-008](../../decisions/ADR-008-durable-job-recovery-and-callback-outbox.md) — proposed durable boundary.
2. [Apex decisions](01-apex-decisions-required.md) — unresolved integration and product contract.
3. [Infrastructure decisions](02-infrastructure-decisions-required.md) — unresolved operational ownership.
4. [Implementation plan](03-implementation-plan.md) — one-slice-at-a-time sequence and stop boundaries.

## Approved direction, not approved activation

The smallest proposed boundary uses one Super-7-owned PostgreSQL source of truth, one analysis
worker, fenced attempts, durable result finalization, a transactional callback outbox, and bounded
artifact metadata. Artifact and video bytes stay outside PostgreSQL. Redis, brokers, task
frameworks, Kubernetes, and additional workers are not part of the proposed initial scope.

For durable admission, the proposed MVP interpretation is that `max_queue_size` counts only
`QUEUED` jobs, `RUNNING` capacity is controlled separately by `max_concurrent_analyses=1`, and
terminal jobs consume no queue capacity. Identical retries resolve before capacity rejection, and
new admission plus capacity enforcement is transactional. This interpretation requires human
approval before implementation.

Slice 1 owns only PostgreSQL dependency/configuration, migration tooling, connectivity,
schema-version checks, and isolated tooling verification. Slice 2 owns the first domain migration
for `AnalysisJob` and idempotency, including its indexes, uniqueness constraints, repository, and
`accept_or_get` tests. The raw `idempotencyKey` must be neither logged nor stored directly; an
approved deterministic digest or otherwise protected lookup representation is required, with the
specific approach deferred to security review.

No executable work may begin merely because this package exists. Slice 0 decisions and the stated
prerequisites for the selected slice must be approved first. Only one implementation slice may be
implemented and reviewed at a time. Slice 2 builds only the persistence boundary. Public route
activation is forbidden in Slice 9 and before the Slice 10 disposable-database crash matrix is
green. It is reserved for a separately reviewed Slice 11 boundary after all applicable slices,
Apex and infrastructure decisions, migration/rollback rehearsal, and explicit human authorization
are green and approved.

## Explicit non-goals

- Runtime, test, schema, migration, dependency, configuration, workflow, or deployment changes.
- Production or database creation or server access.
- Apex code or database changes.
- Callback delivery, model inference, or load testing.
- CV, tracking, GSR, scoring, or public rating changes.
- An exactly-once execution or delivery claim.
- A public status, result, cancellation, or redrive endpoint.
