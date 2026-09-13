> Status: Proposed implementation sequence. No slice is authorized by this document.

# Sprint 2 ordered implementation plan

Only one slice may be implemented and reviewed at a time. A later slice must not be pulled into an
earlier pull request for convenience. Each implementation commit must finish green; tests may be
made red locally first but deliberately failing tests must not be committed to a shared branch.

## Slice 0: Contract and invariant approval

- **Goal:** Approve the required entries in the [Apex](01-apex-decisions-required.md) and
  [infrastructure](02-infrastructure-decisions-required.md) registers and freeze the analysis and
  delivery invariants in [ADR-008](../../decisions/ADR-008-durable-job-recovery-and-callback-outbox.md).
- **Authorized in principle:** ADRs, contracts, decision registers, and future contract-test
  fixtures only.
- **Red first:** Contract cases for same-key/same-request, same-key/conflicting-request, canonical
  IDs, stable callback event identity, duplicate acknowledgement, and separate analysis/delivery
  states must demonstrate incompatibility with the current four-field API before implementation.
- **Invariants:** Recommendations are not treated as Apex approval; no runtime field is introduced
  without an approved owner and compatibility rule.
- **Non-goals:** Database libraries, schema, migrations, route changes, or production activation.
- **Prior approval:** Human acceptance of the discovery package; named Apex and infrastructure
  owners for every blocking row.
- **Rollback:** Revert only the new proposal documents; preserve historical decisions.
- **Branch/commit:** `docs/sprint-2-contract-approval`; one documentation-only commit.

## Slice 1: PostgreSQL foundation and migration tooling

- **Goal:** Add the smallest reviewed PostgreSQL dependency/configuration boundary, migration
  tooling, connectivity, and schema-version checks needed by later slices without creating domain
  tables or wiring production flow.
- **Authorized in principle:** Dependency/lock files, database configuration, a new persistence
  connectivity package, migration-tool metadata/bootstrap, and isolated tooling verification.
- **Red first:** Missing/invalid configuration, unavailable connectivity, isolated migration-tool
  invocation against an empty disposable schema, least-privilege assumptions, and schema-version
  mismatch.
- **Invariants:** No default credentials; no Apex tables; no `AnalysisJob`, idempotency, attempt,
  result, callback, or artifact domain tables; current route and worker remain active and unchanged.
- **Non-goals:** Domain migrations, domain constraints or indexes, `accept_or_get`, worker claims,
  callbacks, cancellation, deployment, or Compose.
- **Prior approval:** Infrastructure database/schema, secrets, migration owner, backup posture, and
  connection budget.
- **Rollback:** Revert the unused connectivity and tooling boundary; no domain data or domain table
  downgrade exists in this slice.
- **Branch/commit:** `feat/sprint-2-postgres-foundation`; separate dependency/configuration and
  connectivity/migration-tooling commits.

## Slice 2: Atomic durable `accept_or_get` persistence boundary

- **Goal:** Atomically create or retrieve one durable `QUEUED` job for a caller-scoped idempotency
  key and verify bounded admission entirely below the public route.
- **Authorized in principle:** The first reversible domain migration for `AnalysisJob` and
  idempotency, its indexes and uniqueness constraints, job/idempotency persistence models and
  repository, request fingerprinting, clock/ID seams, and isolated repository/integration tests. No
  attempt, result, callback, or artifact table and no public route wiring or activation is
  authorized.
- **Red first:** Concurrent identical requests create one job; mismatch creates no row and reports
  conflict; rollback leaves no accepted job; response loss followed by retry returns the same job;
  fresh upgrade and empty-database downgrade of the first domain migration; required domain columns,
  indexes, and uniqueness constraints; terminal jobs consume no queue capacity; concurrent new
  admissions cannot exceed queue capacity; the raw idempotency key is absent from stored rows and
  captured logs.
- **Invariants:** Canonical `jobId`; immutable fingerprint; initial `QUEUED`; `max_queue_size` counts
  only `QUEUED` jobs; `RUNNING` is separately limited by `max_concurrent_analyses=1`; terminal jobs
  consume no queue capacity; identical retry lookup precedes capacity rejection; new admission and
  capacity enforcement are transactional; invalid admission ordinarily creates no job. The raw
  `idempotencyKey` is neither logged nor stored directly; persistence uses an approved deterministic
  digest or otherwise protected representation suitable for uniqueness lookup. No algorithm is
  selected before security review. These capacity semantics remain proposed until human approval.
- **Non-goals:** Worker recovery, result storage, callback dispatcher, cancellation, artifacts,
  public route wiring or activation, public status/result endpoints, or more workers.
- **Prior approval:** Slice 0 key creator/reuse, caller scope, fingerprint, intentional re-analysis,
  ID compatibility, proposed capacity semantics, and security review of the protected lookup
  approach; Slice 1 green.
- **Rollback:** The current route remains untouched. Revert only the unused persistence adapter and
  downgrade the domain migration only on an empty disposable database; preserve any committed
  records needed by later reviewed slices.
- **Branch/commit:** `feat/sprint-2-durable-admission`; domain migration/constraints commit followed
  by repository/behavior tests, with no route changes.

## Slice 3: Worker claim, lease, heartbeat, fencing, and recovery

- **Goal:** Let the existing one-worker policy claim durable jobs, maintain ownership, and recover
  expired attempts.
- **Authorized in principle:** Attempt repository, worker-parent claim loop, lease heartbeat,
  recovery service, safe clocks, and disposable-database concurrency tests.
- **Red first:** Two claimers race for one job; lease renewal; expiry/requeue; stale heartbeat and
  stale finalization rejection; retryable and non-retryable outcomes; maximum-attempt enforcement;
  attempt exhaustion; restart finds queued and expired work.
- **Invariants:** One current claim per job; no transaction spans analysis; every mutation matches
  the current fence; retry uses the same job and resolved version.
- **Super-7 decision boundary:** Before implementation, Super-7 must approve retryable versus
  non-retryable attempt classification, maximum attempts, the durable exhaustion outcome, and the
  named Super-7 runtime/operations owner for lease and heartbeat configuration approval after
  measurement. No numeric value is selected here; externally visible exhaustion behavior remains
  subject to the approved Apex contract.
- **Non-goals:** Additional workers, broker, hard child kill, result finalization, callback delivery,
  or production capacity testing.
- **Prior approval:** Source-video availability; the Super-7 retry, exhaustion, and configuration
  ownership decisions above; infrastructure availability and pool budget; slices 1–2 green.
- **Rollback:** Stop new claims; preserve durable `QUEUED`/`RUNNING` records for reconciliation.
- **Branch/commit:** `feat/sprint-2-worker-recovery`; claim/fence and recovery commits reviewed
  separately.

## Slice 4: Fenced result finalization and transactional callback outbox

- **Goal:** Persist one terminal result or failure and, when the approved contract requires one,
  create its callback event in the same fenced transaction, separating analysis completion from
  delivery.
- **Authorized in principle:** Result and outbox repositories, finalization transaction, terminal
  payload builder, and crash-boundary tests.
- **Red first:** Result exactly once; stale attempt cannot finalize; failure rolls back result and
  outbox together; crash before/after commit; callback-phase cancellation cannot rewrite
  `COMPLETED`.
- **Invariants:** `COMPLETED` requires a durable result; terminal state and any contract-required
  outbox event commit atomically; delivery state never changes analysis state.
- **Non-goals:** Sending callbacks, redrive, cancellation API, artifacts, or result-query endpoint.
- **Prior approval:** Callback event identity and terminal payload compatibility; slice 3 green.
- **Rollback:** Stop finalizers before reverting readers; preserve all committed terminal rows and
  outbox events.
- **Branch/commit:** `feat/sprint-2-result-outbox`; one transaction-focused commit.

## Slice 5: Independent durable callback dispatcher

- **Goal:** Deliver due outbox events independently of analysis slots and resume safely after
  restart.
- **Authorized in principle:** Dispatcher role, outbox claim lease, callback-attempt records,
  retry scheduling, current transport adapter, and injected receiver tests.
- **Red first:** Restart during retry; lost acknowledgement; duplicate dispatch claim; stable event
  ID across attempts; delivered/exhausted transitions; completed analysis remains completed.
- **Invariants:** At-least-once delivery; one current dispatcher fence; same immutable payload and
  event ID for retry; no inline callback in the analysis finalization path.
- **Non-goals:** Exactly-once delivery, new public endpoint, unapproved authentication algorithm,
  broker, or production callback.
- **Prior approval:** Apex duplicate acknowledgement, authentication/replay, retry/redrive window,
  and authoritative-state decisions; slice 4 green.
- **Rollback:** Stop dispatcher claims while retaining all pending/retrying events.
- **Branch/commit:** `feat/sprint-2-callback-dispatcher`; claim/state commit followed by transport
  integration commit.

## Slice 6: Durable cancellation after contract approval

- **Goal:** Persist approved cancellation intent and resolve cancellation/completion/recovery races.
- **Authorized in principle:** Conditional job transitions, worker cancellation observation,
  cancellation audit fields, and contract tests. A public endpoint is not implied.
- **Red first:** Queued cancellation; running request versus completion; lease expiry with cancellation
  pending; duplicate cancellation; completed job cannot become cancelled; approved callback policy.
- **Invariants:** Process shutdown is not business cancellation; a terminal result is immutable;
  cancellation is authenticated and fenced.
- **Non-goals:** Inventing cancellation authority, forced native-process termination, or changing
  callback semantics without Apex approval.
- **Prior approval:** Cancellation authority, running-race behavior, and cancellation callback row
  in the Apex register; slice 3 and, where callbacks apply, slice 5 green.
- **Rollback:** Disable new cancellation requests while preserving already committed terminal state.
- **Branch/commit:** `feat/sprint-2-durable-cancellation`; one behavior-and-tests commit.

## Slice 7: Artifact ownership and manager-recreation reconciliation

- **Goal:** Make retained diagnostic ownership, pruning, and failed cleanup recoverable without
  storing artifact bytes in PostgreSQL.
- **Authorized in principle:** Artifact metadata repository, attempt-scoped safe naming, cleanup
  claims, startup reconciliation, retention cleanup, and filesystem-failure tests.
- **Red first:** Manager recreation preserves ordering; expired staging cleanup; unregistered orphan
  grace; failed deletion remains retryable; stale attempt cannot publish; path escape is rejected;
  unavailable durable root follows the approved fallback.
- **Invariants:** Result success does not depend on debug retention; deletion is confined to the
  reviewed root; physical deletion precedes durable `DELETED`; bytes remain outside PostgreSQL.
- **Non-goals:** Object-store selection, unbounded diagnostic retention, general cache infrastructure,
  or changing scoring artifacts.
- **Prior approval:** Artifact-root availability, fallback behavior, retention period/count, and
  infrastructure access ownership; slices 1 and 3 green.
- **Rollback:** Disable retention and cleanup claims; preserve registered paths for later
  reconciliation rather than deleting metadata.
- **Branch/commit:** `feat/sprint-2-artifact-recovery`; metadata/publish and reconciliation/prune
  commits separated.

## Slice 8: Stateless API and operational readiness

- **Goal:** Ensure API replacement cannot lose authoritative state and expose safe database,
  migration, worker, queue, lease, and callback-backlog readiness.
- **Authorized in principle:** Role composition, health/readiness adapters, bounded connection use,
  safe lifecycle logging/metrics, and API-replacement tests.
- **Red first:** Replace API after acceptance; database outage before acceptance; schema mismatch;
  worker unavailable; stale lease/backlog visibility; cache loss does not lose a job or result.
- **Invariants:** API memory is never the only accepted-job copy; liveness and readiness are distinct;
  health checks do not run inference.
- **Non-goals:** Additional API/worker replicas, deployment, autoscaling, dashboards, or SLA claims.
- **Prior approval:** Infrastructure availability and connection budget; slices 2–5 green.
- **Rollback:** Keep durable writers/readers compatible; disable new role wiring without deleting
  state.
- **Branch/commit:** `feat/sprint-2-stateless-readiness`; role composition and health changes in
  separate commits.

## Slice 9: Legacy compatibility and cleanup

- **Goal:** Prepare compatibility and cleanup for a future move from the current four-field,
  non-idempotent route without activating the durable contract or claiming guarantees for legacy
  requests.
- **Authorized in principle:** Versioned schema compatibility, `jobId`/`analysisId` aliasing, feature
  control that remains disabled, an in-memory queue/state retirement plan, and bounded legacy
  cleanup. No production route selection or activation is authorized.
- **Red first:** Legacy and V1 request matrix; terminal duplicate response; conflict response;
  compatibility reversal; disabled durable-route selection; cancelled queue accounting; bounded
  terminal history; no double dispatch.
- **Invariants:** Legacy behavior is explicitly labelled; one request never enters both queues;
  durable records survive rollback; durable route selection remains disabled; no result/status
  endpoint is implied.
- **Non-goals:** Silent mandatory-field rollout, destructive data migration, deployment, public
  production route activation, or retirement of the active in-memory path.
- **Prior approval:** Apex compatibility and proposed retirement decisions, retry window, retention,
  and all applicable earlier slices green.
- **Rollback:** Remove unused compatibility selection while durable records and migrations remain
  readable; the active route is unchanged.
- **Branch/commit:** `feat/sprint-2-legacy-compatibility`; compatibility and cleanup commits reviewed
  independently, with no activation commit.

## Slice 10: Disposable-database crash-matrix verification

- **Goal:** Prove recovery at each lifecycle boundary using only disposable local/test resources.
- **Authorized in principle:** Deterministic fault injection, disposable PostgreSQL fixtures,
  process-restart harnesses, and verification documentation.
- **Red first:** Death before/after acceptance commit, claim, heartbeat, result/outbox commit,
  callback attempt/acknowledgement, cancellation race, and artifact publication/deletion.
- **Invariants:** No accepted committed job becomes unknown; stale attempts cannot finalize;
  callbacks remain at least once with one event identity; no production system is used.
- **Non-goals:** Production migration, server testing, model inference, capacity claims, or deployment.
- **Prior approval:** All implemented slices green; disposable database procedure and cleanup
  approved.
- **Rollback:** Remove only verified disposable resources; test harness changes are independently
  revertible.
- **Branch/commit:** `test/sprint-2-crash-matrix`; fault-harness and documented-result commits
  separated.

## Slice 11: Separately reviewed final activation and cutover

- **Goal:** Activate the approved durable route only after the complete implementation and crash
  evidence demonstrate a recoverable end-to-end path.
- **Authorized in principle:** Final route selection, reviewed production migration/cutover and
  rollback procedures, activation controls, and an evidence record. Activation is the final
  independently reviewable commit.
- **Red first:** Pre-activation compatibility and readiness gates; migration and rollback rehearsal;
  accepted-job recovery through callback completion; duplicate request and callback handling;
  activation failure and rollback without deleting durable state.
- **Invariants:** No activation before Slice 10 is green; one request enters only the selected path;
  no accepted durable job is deleted by rollback; the final commit contains activation only.
- **Non-goals:** New domain schema, lifecycle behavior, callback semantics, artifact behavior,
  concurrency expansion, deployment-platform redesign, or fixes discovered during rehearsal.
- **Prior approval:** Every applicable Slice 0–9 implementation and Slice 10 crash matrix green and
  approved; all applicable Apex and infrastructure decisions approved; production migration and
  rollback rehearsed; explicit human authorization recorded.
- **Rollback:** Restore the previously approved route selection without deleting durable records or
  reversing migrations through data loss; follow the rehearsed rollback procedure.
- **Branch/commit:** `feat/sprint-2-final-activation`; preparation/evidence may precede it, but the
  production activation change is the final independent commit.

## First implementation recommendation

After the independently reviewed Slice 1 tooling/connectivity foundation is green, the first domain
implementation task should be only the Slice 2 **PostgreSQL-backed, concurrency-tested
`accept_or_get`** boundary.

Future scope is limited to:

- the first domain migration for `AnalysisJob` and idempotency, owned by Slice 2;
- required domain indexes and uniqueness constraints;
- canonical `jobId`;
- caller-scoped idempotency uniqueness;
- an approved deterministic digest or otherwise protected idempotency lookup representation, with
  no raw key logging or storage and no algorithm selected before security review;
- immutable request fingerprint;
- initial `QUEUED` state;
- atomic capacity/admission decision;
- concurrent duplicate tests; and
- transaction-failure tests.

Explicitly excluded are worker recovery, callback dispatcher, cancellation, artifact reconciliation,
public route wiring or activation, public status/result endpoints, and additional worker processes.
Any future public production route activation belongs only to Slice 11 after all applicable slices,
including the Slice 10 disposable-database crash matrix, are green and approved. Completing this
task does not authorize Slice 3 or any later slice.
