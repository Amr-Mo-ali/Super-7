# ADR-008: Durable job recovery and callback outbox

## Status

Accepted architecture — local implementation authorized; interoperability and production activation pending

The project owner reports Apex approval of the reviewed Sprint 2 direction and Super-7 review
corrections. Super-7 accepts the internal durability architecture defined here. No itemized Apex
response, exact external contract values, or operational evidence accompany that report in this
repository; the [Apex decision register](../workplans/sprint-2/01-apex-decisions-required.md)
therefore remains open for interoperability evidence. Those missing external values do not block
local Slice 1. They remain gates for the first slice or shared integration boundary that consumes
them and for Slice 11 production cutover. Acceptance does not authorize production activation.

This ADR does not change current behavior, approve production activation, or alter the historical
status of [ADR-004](ADR-004-analysis-job-lifecycle-and-idempotency.md),
[ADR-005](ADR-005-durable-job-storage-and-worker-architecture.md),
[ADR-006](ADR-006-controlled-concurrency-mvp.md), or
[ADR-007](ADR-007-process-execution-boundary.md). ADR-008 supersedes ADR-004 only regarding the
direct `QUEUED -> FAILED` transition for the durable implementation; ADR-004 remains historical
authority outside that narrow conflict. ADR-008 consolidates the accepted Sprint 2 architecture
at the repository state recorded in the
[Sprint 2 discovery](../workplans/sprint-2/README.md).

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

## Decision

Use PostgreSQL as the sole authoritative durable store for accepted jobs, idempotency bindings,
execution attempts, results, callback delivery, and retained-artifact metadata. A job in durable
`QUEUED` state is the dispatch intent; no broker is required for correctness. Bounded polling may
be used to find work, with database notifications considered only as a future wake-up optimization.

The initial concurrency policy remains one analysis worker and one active analysis. Sprint 2 does
not authorize an additional worker or a process-count change.

### Current and accepted execution boundaries

The current default executable production composition in [`create_app`](../../src/main.py) creates
a [`ProcessAnalysisPool`](../../src/services/process_analysis_pool.py), passes its
`create_process_analysis_job_processor` adapter to `AnalysisWorker`, starts the pool during lifespan
startup, and exposes the module-level app from that composition. `ProcessAnalysisPool` creates a
`ProcessPoolExecutor` with `max_workers=1` and `multiprocessing.get_context("spawn")`, submits the
top-level [`run_child_analysis`](../../src/services/process_entrypoint.py) callable, and validates
the serialized result in the parent. This is executable composition evidence, not production-server
validation. Some source docstrings still describe the process adapter or child entry point as
"future" or "unused"; those comments are stale relative to the `main.py` call graph.

The retained `create_analysis_job_processor` in [`routes.py`](../../src/api/routes.py) is the
legacy/in-process alternative and is not selected by `main.py`. That alternative reaches
[`AnalysisExecutor`](../../src/concurrency/executor.py), where CPU analysis uses
`asyncio.to_thread`. The two other production-source `asyncio.to_thread` call sites offload
[`CallbackService`](../../src/services/callback_service.py) transport and the process pool's blocking
shutdown; neither runs the active CPU analysis pipeline. The active default analysis path waits on
the submitted process-pool future.

ADR-007 is the accepted execution-boundary decision and the current default composition implements
its one-worker `ProcessPoolExecutor`/`spawn`, child-owned analysis, and parent-owned callback model.
Its descriptions of thread execution as current and the pool as future are historical context. The
custom spawn supervisor rejected by ADR-007 has no implementation in the current source tree; it is
not the `ProcessAnalysisPool` and is not proposed by this ADR.

Local persistence foundations may be implemented against disposable PostgreSQL without waiting for
Apex internal tests or real staging/production infrastructure. Exact external contract values are
required before the first integration boundary that consumes them. Real infrastructure and its
evidence are required before staging integration or Slice 11 activation. Building the persistence
boundary does not authorize public route wiring or activation:

1. Validate the request and resolve immutable versions.
2. In one transaction, derive and bind the approved protected lookup representation from the
   caller-scoped idempotency key, create or retrieve the canonical `jobId`, record the immutable
   request fingerprint, and persist initial `QUEUED` state.
3. Return HTTP 202 only after a new-job transaction commits. An identical retry returns the existing
   job; a conflicting fingerprint creates nothing.

The raw `idempotencyKey` is neither logged nor stored directly. The accepted persistence boundary
stores a deterministic digest or otherwise protected representation suitable for caller-scoped
uniqueness lookup. The exact protection or digest algorithm, keying, and rotation approach require
security review and are not selected by this ADR.

### Accepted initial MVP queue-capacity semantics

The accepted initial Super-7 durable contract separates waiting work from active-analysis capacity:

- `max_queue_size` counts jobs whose durable analysis state is `QUEUED`.
- `RUNNING` capacity is controlled separately by `max_concurrent_analyses=1`.
- Terminal jobs do not consume queue capacity.
- An identical idempotent retry returns its existing job before capacity rejection is considered.
- New-job creation and queue-capacity enforcement occur in one transaction that prevents concurrent
  admissions from exceeding the approved limit.
- Invalid admission ordinarily fails before a durable job is created.

Exact numeric limits remain configuration and measurement decisions. These semantics do not claim
measured production capacity or authorize production activation.

## Durable responsibilities

| Record | Responsibility |
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
policy allows another attempt. Invalid admission fails before a durable job is created. After a
durable job exists, a terminal pre-analysis failure must be owned by a claimed and fenced attempt;
the job therefore transitions `QUEUED -> RUNNING -> FAILED`. No service may terminalize a durable
job directly from `QUEUED` to `FAILED`. Terminal transitions require a fenced transaction, and a
stale attempt cannot overwrite a newer attempt or terminal result. This narrower rule supersedes
ADR-004's direct `QUEUED -> FAILED` allowance for the durable implementation.

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
as a successful no-op. Super-7 may define and test its local delivery contract with deterministic
vectors in the applicable callback slice. Matching Apex acknowledgement, retry, redrive,
authentication, and replay behavior remains an interoperability and cutover gate in the
[Apex decision register](../workplans/sprint-2/01-apex-decisions-required.md), not a Slice 1 gate.

This architecture makes no exactly-once computation or exactly-once network-delivery claim.

## Artifact ownership and manager recreation

The current manager keeps session and retention ordering in memory. Accepted artifact ownership is
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
- SQLite may be considered only for isolated adapter tests; it is not the accepted production
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
may be implemented and reviewed at a time. The first implementation slice is Slice 1 PostgreSQL
foundation: dependency and migration tooling, typed configuration, connectivity, bounded pool
lifecycle, schema-version checks, and disposable-database verification, with no domain tables. The
first domain-persistence slice is Slice 2: the `AnalysisJob` and idempotency migration plus the
PostgreSQL-backed, concurrency-tested `accept_or_get` boundary. Slice 2 must store only an approved
protected key representation and performs no public route wiring or activation. Local Slice 1 is
authorized; every later slice remains subject to its own prerequisites, and Slice 11 remains the
sole production activation boundary.
