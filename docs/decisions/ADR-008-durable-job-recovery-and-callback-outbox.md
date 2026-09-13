# ADR-008: Durable job recovery and callback outbox

## Status

Proposed — blocked on Apex and infrastructure decisions

This ADR does not change current behavior, approve production activation, or supersede the
historical status of [ADR-004](ADR-004-analysis-job-lifecycle-and-idempotency.md),
[ADR-005](ADR-005-durable-job-storage-and-worker-architecture.md),
[ADR-006](ADR-006-controlled-concurrency-mvp.md), or
[ADR-007](ADR-007-process-execution-boundary.md). It consolidates the proposed Sprint 2 boundary
at the repository state recorded in the [Sprint 2 discovery](../workplans/sprint-2/README.md).

## Context

Current `POST /analyze` acceptance places a job into a process-local bounded queue and returns HTTP
202. The queue, lifecycle state, result, cancellation intent, child result, callback retry state,
and retained-artifact ordering have no durable source of truth. A process restart can therefore
make an accepted job permanently unknown, and a retry of the same Apex request creates a new
analysis identity and duplicate work. Analysis completion is also finalized only after inline
callback delivery returns, so callback-phase cancellation can overwrite an already completed
analysis with `CANCELLED`.

The current behavior and evidence are described in the
[system audit](../workplans/system-audit-2026-09-04.md), the
[canonical runtime handoff](../handoff/system-and-runtime.md), and the
[proposed V1 job contract](../contracts/analysis-job-contract-v1.md). These sources describe current
and earlier proposed behavior; none proves durable operation.

## Proposed decision

Use PostgreSQL as the sole authoritative durable store for accepted jobs, idempotency bindings,
execution attempts, results, callback delivery, and retained-artifact metadata. A job in durable
`QUEUED` state is the dispatch intent; no broker is required for correctness. Bounded polling may
be used to find work, with database notifications considered only as a future wake-up optimization.

The initial concurrency policy remains one analysis worker and one active analysis. Sprint 2 does
not authorize an additional worker or a process-count change. The existing spawned child boundary
may later remain the CPU-execution boundary, while durable claim and recovery ownership stays in
the worker parent.

The future durable admission contract is transactional only after the required Apex and
infrastructure decisions are approved. Building its persistence boundary does not authorize public
route wiring or activation:

1. Validate the request and resolve immutable versions.
2. In one transaction, derive and bind the approved protected lookup representation from the
   caller-scoped idempotency key, create or retrieve the canonical `jobId`, record the immutable
   request fingerprint, and persist initial `QUEUED` state.
3. Return HTTP 202 only after a new-job transaction commits. An identical retry returns the existing
   job; a conflicting fingerprint creates nothing.

The raw `idempotencyKey` is neither logged nor stored directly. The proposed persistence boundary
stores a deterministic digest or otherwise protected representation suitable for caller-scoped
uniqueness lookup. The exact protection or digest algorithm, keying, and rotation approach require
security review and are not selected by this ADR.

### Proposed MVP queue-capacity semantics

The accepted controlled-concurrency documents separate waiting work from active-analysis capacity,
but the durable transactional interpretation below remains proposed until human approval:

- `max_queue_size` counts jobs whose durable analysis state is `QUEUED`.
- `RUNNING` capacity is controlled separately by `max_concurrent_analyses=1`.
- Terminal jobs do not consume queue capacity.
- An identical idempotent retry returns its existing job before capacity rejection is considered.
- New-job creation and queue-capacity enforcement occur in one transaction that prevents concurrent
  admissions from exceeding the approved limit.
- Invalid admission ordinarily fails before a durable job is created.

## Durable responsibilities

| Record | Proposed responsibility |
|---|---|
| Job | Canonical `jobId`, caller-scoped protected idempotency lookup binding, immutable request fingerprint, resolved versions, analysis state, cancellation fields, and lifecycle timestamps. |
| Analysis attempt | Attempt number, worker identity, lease token/fence, lease expiry, heartbeat, timestamps, and safe outcome classification. |
| Analysis result | One immutable finalized result or terminal-failure representation per job, with schema and implementation versions. |
| Callback outbox | One stable `callbackEventId` and immutable terminal payload per job, independent delivery state, next-attempt time, dispatcher lease, and bounded retry/redrive metadata. |
| Callback attempt | Attempt number, timestamps, acknowledgement class, and safe error classification. |
| Artifact metadata | Job/attempt ownership, safe relative storage key, byte count, publication state, retention order/deadline, and cleanup ownership. |

PostgreSQL must not store video bytes, debug videos, rendered frames, model files, or unbounded
diagnostic artifacts. Apex-owned source videos remain outside Super-7. Artifact bytes remain on an
explicitly managed artifact root or future object store; PostgreSQL stores only bounded metadata.

## Separate state machines

Analysis state:

```text
QUEUED -----> RUNNING -----> COMPLETED | FAILED | CANCELLED
   |             |
   |             +---------> QUEUED
   +-----------------------> CANCELLED
```

`RUNNING -> QUEUED` is permitted only for an interrupted or retryable attempt while the retry
policy allows another attempt. No `QUEUED -> FAILED` transition is proposed: invalid admission
ordinarily fails before job creation, and any future pre-execution terminal-failure policy requires
an explicit decision. Terminal transitions require a fenced transaction. A stale attempt cannot
overwrite a newer attempt or terminal result.

Callback-delivery state:

```text
NOT_READY -> PENDING -> RETRYING -> DELIVERED | EXHAUSTED
                  \----------------> DELIVERED | EXHAUSTED
```

When the approved contract requires a callback, the terminal result and its `PENDING` callback event
are created atomically. Callback failure, cancellation, dispatcher restart, or exhaustion never
changes `COMPLETED` analysis to `FAILED` or `CANCELLED`.

## Claims, leases, and recovery

- A worker claims one eligible job in a short transaction and does not hold that transaction while
  running video analysis.
- The claim creates an attempt and assigns a lease token or monotonically fenced generation.
- The owning worker heartbeats before lease expiry. Lease and heartbeat values require later
  measurement and configuration approval; no duration is selected here.
- Recovery records an expired attempt as interrupted and requeues the same job when policy permits.
- Every heartbeat, requeue, cancellation, and finalization conditionally matches the current
  attempt fence. Losing a lease can cause duplicate computation, but it cannot cause duplicate
  authoritative finalization.
- A process or host restart never infers completion from memory or from an artifact directory.

Before Slice 3 implementation, Super-7 must approve which attempt outcomes are retryable versus
non-retryable, the maximum-attempt policy, the durable exhaustion outcome, and the named Super-7
runtime/operations owner who approves lease and heartbeat configuration after measurement. No
numeric retry, lease, or heartbeat value is selected by this ADR. Any externally visible mapping of
exhaustion remains subject to the approved Apex contract.

## Callback delivery semantics

Delivery is at least once, not exactly once. A dispatcher may resend after losing an acknowledgement.
Every automatic retry and approved redrive reuses the durable `callbackEventId`. Safe duplicate
delivery therefore requires Apex to persist that identity and acknowledge an already applied event
as a successful no-op. Exact acknowledgement, retry, redrive, authentication, and replay behavior
remain blocked in the [Apex decision register](../workplans/sprint-2/01-apex-decisions-required.md).

This proposal makes no exactly-once computation or exactly-once network-delivery claim.

## Artifact ownership and manager recreation

The current manager keeps session and retention ordering in memory. Proposed artifact ownership is
attempt-scoped and recoverable from durable metadata. Cleanup must claim an eligible artifact record,
prove that its relative path remains beneath the configured root, and record physical deletion only
after it succeeds. Failed deletion remains retryable and observable. Startup reconciliation must
handle expired staging records and unregistered directories only under a reviewed grace and path
policy.

If durable artifact storage is unavailable, retained diagnostic artifacts must be disabled or
explicitly classified as disposable. Analysis results and callbacks must not depend on retained
debug artifacts.

## Why PostgreSQL only

PostgreSQL supplies the transaction, uniqueness, row-locking, fencing, inspection, retention, and
backup boundary needed for acceptance, result finalization, and an outbox. Adding Redis or a broker
would not remove this database requirement and would introduce a second correctness and recovery
boundary. A task framework would add lifecycle and serialization behavior before a measured need.

## Deferred and rejected alternatives

- Redis-only is rejected as the source of truth for accepted jobs, results, idempotency, or delivery.
- A broker, Celery, Kafka, and RabbitMQ are deferred until measured PostgreSQL claim or backlog
  behavior cannot meet an approved objective.
- Kubernetes, autoscaling, multiple hosts, and additional analysis workers are deferred.
- SQLite may be considered only for isolated adapter tests; it is not the proposed production
  coordination boundary.
- The existing in-memory queue cannot provide durable acceptance and is rejected as the future
  source of truth.
- Exactly-once execution and network delivery are rejected as guarantees. Fencing and idempotency
  constrain effects while allowing repeated computation or delivery after ambiguous failures.

## Consequences and risks

Positive consequences:

- Accepted jobs, results, and delivery state survive API, worker, and manager recreation.
- API instances can be replaced without owning the only copy of accepted work.
- Idempotency, result finalization, and callback creation gain explicit transaction boundaries.
- Analysis execution and callback delivery no longer consume one shared terminal-state transition.

Costs and risks:

- PostgreSQL availability becomes required for durable admission and worker coordination.
- Migrations, backups, restore tests, connection budgets, retention, and secrets need explicit owners.
- An expired lease can cause expensive duplicate analysis even though stale finalization is fenced.
- Apex must retain source video long enough for recovery and must deduplicate callback events.
- Result, idempotency, callback, and artifact metadata contain potentially sensitive references and
  require least-privilege access and bounded retention.
- Artifact metadata cannot make worker-local filesystem bytes durable across host replacement.

## Cutover and rollback constraints

- Slice 2 is persistence-only and must not wire or activate a public route. Slice 9 prepares legacy
  compatibility and cleanup but must not activate production. No public production route may return
  durable-acceptance semantics until the durable consumer/recovery path, fenced result finalization,
  every contract-required callback-delivery component, all other applicable slices, and the Slice 10
  disposable-database crash matrix are green and approved. Activation belongs only to the separately
  reviewed Slice 11 cutover boundary after Apex, infrastructure, rehearsal, and human approvals.
- Legacy `analysisId` compatibility and requests without idempotency require an explicit migration
  decision; documentation must not imply they are idempotent.
- A migration must be backward compatible with the currently deployed application until the
  reviewed cutover point. Rollback must not delete accepted jobs or downgrade through irreversible
  data loss.
- Worker, callback, cancellation, and artifact slices remain disabled until their own tests and
  prerequisites are approved.
- Production database creation, migration execution, deployment, and rollback are separate future
  operational actions.

## Implementation boundary

Only one slice in the [Sprint 2 implementation plan](../workplans/sprint-2/03-implementation-plan.md)
may be implemented and reviewed at a time. The first recommended implementation is limited to a
PostgreSQL-backed, concurrency-tested `accept_or_get` persistence boundary. Slice 1 supplies only
tooling and connectivity; Slice 2 owns the first `AnalysisJob` and idempotency domain migration and
must store only an approved protected key representation. No public route wiring or activation is
included. This ADR does not authorize that implementation.
