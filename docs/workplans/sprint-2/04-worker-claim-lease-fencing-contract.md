# Worker claim, lease, fencing, and recovery contract

> Status: Approved for RED-F contract implementation
> Runtime activation: Not approved
> Approval date: 2026-10-06
> Base commit: `634f0d4872ae6d3c13467eda25bd23d2c9ab1a47`
> Branch: `feat/sprint-2-worker-claim-lease-fencing`

## Purpose and approval boundary

This document freezes the inactive worker claim, lease, fencing, failure-resolution, and recovery
contract required before RED-F. It is the implementation-ready authority for the RED-F/G/H/I
sequence described by the [Sprint 2 implementation plan](03-implementation-plan.md).

Approval covers only:

- RED-F domain values, service outcomes, public port, and operation contracts;
- RED-G reversible durable schema and migration contract;
- RED-H deterministic Psycopg transaction behavior; and
- RED-I disposable-PostgreSQL concurrency and recovery evidence.

This approval does not authorize runtime composition, a public route change, a production migration,
deployment, or worker activation. The active runtime remains the existing process-local queue, one
lifespan-owned worker, and one spawned analysis child. PostgreSQL worker persistence, a durable
callback outbox, successful durable finalization, and durable cancellation are not active.

### Non-goals

- No successful result finalizer, result table, callback outbox, or callback dispatcher.
- No durable cancellation API or cancellation authority decision.
- No public HTTP, callback, scoring, identity, or Apex contract change.
- No runtime configuration values or production defaults.
- No additional worker, execution slot, process, broker, Redis, task framework, or Kubernetes.
- No session-long database lock, transaction, or checked-out connection during analysis.
- No exactly-once computation or delivery claim.
- No production database access, migration, deployment, or activation.

## Existing accepted durability constraints

- PostgreSQL is the authoritative durable coordination store.
- A claim is one short transaction using `FOR UPDATE SKIP LOCKED`.
- Fresh claim order is exactly `accepted_at ASC, job_id ASC`.
- Fresh claims originate only from eligible `QUEUED` jobs.
- Analysis never runs inside a database transaction or while retaining a claim connection.
- One execution-slot incarnation receives one opaque UUID for its lifetime.
- A process may later contain multiple execution slots, each with an independent UUID.
- A recreated slot or restarted process receives a new UUID and cannot impersonate its predecessor.
- A successful new claim receives a distinct opaque lease UUID.
- Worker-slot identity and per-claim lease identity are separate.
- Every successful new claim has a positive, monotonically increasing attempt number.
- The attempt number is the fencing generation; heartbeat does not increment it.
- Every job has immutable persisted `analysis_version` and `attempt_limit` policy snapshots.
- A stale, expired, superseded, released, cancelled, or resolved owner cannot mutate current state.
- Recovery is opportunistic and bounded; no dedicated sweeper is required for correctness.
- Retryable failure or interruption may perform `RUNNING -> QUEUED` while attempts remain.
- Terminal failure or exhaustion may perform fenced `RUNNING -> FAILED`.
- Queue admission capacity continues to count only `QUEUED` jobs.
- Recovery is not admission and bypasses queue capacity.
- Successful completion and its result/outbox transaction remain Slice 4.
- Durable business cancellation remains Slice 6.
- The initial activation topology remains one process, one slot, and one active analysis.

The exact durable attempt-status vocabulary is:

```text
ACTIVE
INTERRUPTED
RETRYABLE_FAILURE
TERMINAL_FAILURE
EXHAUSTED
```

## RED-F/G/H/I stage split

| Stage | Authorized work | Explicitly deferred |
|---|---|---|
| RED-F | Domain values, `RUNNING -> QUEUED`, service outcomes, repository Protocol, operation signatures, validation, and no-I/O contract tests | Migrations, SQL, Psycopg, configuration, coordinator, runtime wiring |
| RED-G | One reversible Slice 3 migration, catalog/packaging contract, constraints, indexes, empty apply/downgrade evidence | Repository implementation and activation |
| RED-H | Scripted Psycopg claim, heartbeat, classified failure, recovery, cleanup, error, and admission-compatibility behavior | Real locking evidence, coordinator, activation |
| RED-I | Disposable-PostgreSQL locking, expiry, recovery, fencing, rollback, privilege, and concurrency evidence | Production database, deployment, real source video, runtime activation |

## Exact module tree

```text
src/
|-- domain/
|   |-- analysis_job.py
|   |   |-- RED-F: add RUNNING -> QUEUED
|   |   `-- RED-H: expose persisted analysis_version and attempt_limit
|   `-- analysis_job_attempt.py
|       `-- RED-F: new worker ownership and claim values
|-- services/
|   |-- analysis_job_admission.py
|   |   |-- RED-F: enforce approved worker-safe request text validation before protector calls,
|   |   |   fingerprinting, and repository I/O
|   |   `-- RED-H: extend admission with explicit analysis_version and attempt_limit policy snapshots
|   `-- analysis_job_worker.py
|       `-- RED-F: outcomes, Protocol, operations, and public errors
`-- adapters/
    |-- migrations/postgres/
    |   `-- sprint2_slice3_worker_attempt.py
    |       `-- RED-G only
    |-- psycopg_analysis_job_repository.py
    |   `-- RED-H: Slice 2 column compatibility
    `-- psycopg_analysis_job_worker_repository.py
        `-- RED-H: claim, lease, failure, and recovery adapter

tests/
|-- test_analysis_job_worker_contract.py
|-- test_postgres_worker_attempt_migration_contract.py
|-- test_analysis_job_worker_repository_contract.py
`-- test_postgres_analysis_job_worker_integration.py
```

The first RED-F production module is `src/domain/analysis_job_attempt.py`. RED-F then adds
`src/services/analysis_job_worker.py`. RED-F creates no migration or adapter.

## Exact public exports

### `domain.analysis_job`

```python
__all__ = [
    "AnalysisJob",
    "AnalysisJobAccepted",
    "AnalysisJobCapacityRejected",
    "AnalysisJobExisting",
    "AnalysisJobIdempotencyConflict",
    "AnalysisJobState",
    "NewAnalysisJob",
    "is_analysis_job_transition_allowed",
]
```

### `domain.analysis_job_attempt`

```python
__all__ = [
    "AttemptAuthority",
    "AttemptFailureCode",
    "AttemptStatus",
    "ClaimableAnalysisJob",
    "LeaseDuration",
    "LeaseId",
    "RecoveryBatchSize",
    "WorkerClaim",
    "WorkerSlotId",
]
```

### `services.analysis_job_worker`

```python
__all__ = [
    "AnalysisJobWorkerPersistenceInvariantError",
    "AnalysisJobWorkerPersistenceUnavailable",
    "AnalysisJobWorkerRepository",
    "AttemptExhausted",
    "AttemptFailed",
    "AttemptRequeued",
    "AttemptResolutionAlreadyApplied",
    "ClaimLost",
    "LeaseRenewed",
    "NoJobAvailable",
    "RecoverySummary",
    "WorkerSlotUnavailable",
    "claim_next_analysis_job",
    "finalize_retryable_failure",
    "finalize_terminal_failure",
    "recover_expired_analysis_jobs",
    "renew_analysis_job_lease",
]
```

Helpers, union aliases, regexes, row decoders, SQL constants, and driver types remain private.

## Exact public domain skeletons

These skeletons freeze names, field order, and field types. Bodies shown as ellipses are
specification placeholders, not executable implementation.

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID


class AttemptStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INTERRUPTED = "INTERRUPTED"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    TERMINAL_FAILURE = "TERMINAL_FAILURE"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True, slots=True)
class WorkerSlotId:
    value: UUID


@dataclass(frozen=True, slots=True)
class LeaseId:
    value: UUID


@dataclass(frozen=True, slots=True)
class LeaseDuration:
    value: timedelta


@dataclass(frozen=True, slots=True)
class RecoveryBatchSize:
    value: int


@dataclass(frozen=True, slots=True)
class AttemptFailureCode:
    value: str


@dataclass(frozen=True, slots=True)
class AttemptAuthority:
    job_id: UUID
    attempt_number: int
    worker_slot_id: WorkerSlotId
    lease_id: LeaseId


@dataclass(frozen=True, slots=True)
class ClaimableAnalysisJob:
    job_id: UUID
    video_id: str
    player_id: str
    video_reference: str
    callback_url: str
    analysis_version: str
    attempt_limit: int
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class WorkerClaim:
    authority: AttemptAuthority
    job: ClaimableAnalysisJob
    started_at: datetime
    heartbeat_at: datetime
    lease_expires_at: datetime
```

### Claim execution payload

`ClaimableAnalysisJob` contains every immutable input needed to reconstruct the current parent
analysis boundary without a second unfenced job lookup:

- `job_id` maps to the current analysis identity;
- `video_id` and `player_id` retain the current Apex correlation identifiers;
- `video_reference` maps to current `AnalyzeRequest.video_url` and
  `ChildAnalysisRequest.video_reference`;
- `callback_url` is the exact canonical string stored at admission;
- `analysis_version` is the immutable version selected at admission;
- `attempt_limit` is the immutable retry limit selected at admission; and
- `accepted_at` preserves deterministic queue ordering and audit time.

Before activation, the child-process boundary must receive the persisted `analysis_version`. It
must reject a version it cannot execute rather than substituting its current local setting.

The claim excludes raw idempotency keys, protected lookup bytes, request fingerprints, HMAC
secrets, result/outbox material, and database/process/task objects.

### Target Slice 2 domain compatibility

After RED-G/H, the durable admission values have this exact shape:

```python
@dataclass(frozen=True, slots=True)
class AnalysisJob:
    job_id: UUID
    video_id: str
    player_id: str
    video_reference: str
    callback_url: str
    analysis_version: str
    state: AnalysisJobState
    attempt_limit: int
    accepted_at: datetime


@dataclass(frozen=True, slots=True)
class NewAnalysisJob:
    job_id: UUID
    idempotency_lookup: bytes
    request_fingerprint: bytes
    video_id: str
    player_id: str
    video_reference: str
    callback_url: str
    analysis_version: str
    attempt_limit: int
```

## Exact service outcomes

```python
@dataclass(frozen=True, slots=True)
class NoJobAvailable:
    pass


@dataclass(frozen=True, slots=True)
class WorkerSlotUnavailable:
    worker_slot_id: WorkerSlotId


@dataclass(frozen=True, slots=True)
class ClaimLost:
    authority: AttemptAuthority


@dataclass(frozen=True, slots=True)
class LeaseRenewed:
    authority: AttemptAuthority
    heartbeat_at: datetime
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AttemptRequeued:
    authority: AttemptAuthority
    attempt_limit: int


@dataclass(frozen=True, slots=True)
class AttemptFailed:
    authority: AttemptAuthority
    failure_code: AttemptFailureCode


@dataclass(frozen=True, slots=True)
class AttemptExhausted:
    authority: AttemptAuthority
    attempt_limit: int
    failure_code: AttemptFailureCode


@dataclass(frozen=True, slots=True)
class AttemptResolutionAlreadyApplied:
    authority: AttemptAuthority
    status: AttemptStatus
    failure_code: AttemptFailureCode


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    requeued_count: int
    exhausted_count: int
```

- `WorkerClaim` is returned for either a new claim or an exact valid same-slot claim retry.
- `NoJobAvailable` means the slot is free but no queued job is eligible.
- `WorkerSlotUnavailable` means the supplied slot has a non-returnable active attempt awaiting
  recovery. It contains no lease identity and grants no mutation authority.
- `ClaimLost` is used only by operations whose caller supplied `AttemptAuthority`.
- `RecoverySummary(0, 0)` is the no-eligible-recovery result.
- Slice 3 defines no successful-completion outcome.

## Exact repository Protocol

```python
class AnalysisJobWorkerRepository(Protocol):
    async def claim_next(
        self,
        *,
        worker_slot_id: WorkerSlotId,
        lease_duration: LeaseDuration,
    ) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable: ...

    async def renew_lease(
        self,
        *,
        authority: AttemptAuthority,
        lease_duration: LeaseDuration,
    ) -> LeaseRenewed | ClaimLost: ...

    async def finalize_retryable_failure(
        self,
        *,
        authority: AttemptAuthority,
        failure_code: AttemptFailureCode,
    ) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost: ...

    async def finalize_terminal_failure(
        self,
        *,
        authority: AttemptAuthority,
        failure_code: AttemptFailureCode,
    ) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost: ...

    async def recover_expired(
        self,
        *,
        batch_size: RecoveryBatchSize,
    ) -> RecoverySummary: ...
```

## Exact service-operation signatures

```python
async def claim_next_analysis_job(
    *,
    repository: AnalysisJobWorkerRepository,
    worker_slot_id: WorkerSlotId,
    lease_duration: LeaseDuration,
) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable: ...


async def renew_analysis_job_lease(
    *,
    repository: AnalysisJobWorkerRepository,
    authority: AttemptAuthority,
    lease_duration: LeaseDuration,
) -> LeaseRenewed | ClaimLost: ...


async def finalize_retryable_failure(
    *,
    repository: AnalysisJobWorkerRepository,
    authority: AttemptAuthority,
    failure_code: AttemptFailureCode,
) -> AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost: ...


async def finalize_terminal_failure(
    *,
    repository: AnalysisJobWorkerRepository,
    authority: AttemptAuthority,
    failure_code: AttemptFailureCode,
) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost: ...


async def recover_expired_analysis_jobs(
    *,
    repository: AnalysisJobWorkerRepository,
    batch_size: RecoveryBatchSize,
) -> RecoverySummary: ...
```

Each service operation validates public inputs before I/O, makes exactly one repository call, does
not accept current time, does not retry, and does not classify arbitrary exceptions.

## Exact validation matrix

| Value | Required validation |
|---|---|
| `WorkerSlotId.value` | `type(value) is UUID`; reject nil UUID |
| `LeaseId.value` | `type(value) is UUID`; reject nil UUID |
| `AttemptAuthority.job_id` | `type(value) is UUID`; reject nil UUID |
| `ClaimableAnalysisJob.job_id` | `type(value) is UUID`; reject nil UUID |
| `AnalysisJob.job_id` | `type(value) is UUID`; reject nil UUID |
| `NewAnalysisJob.job_id` | `type(value) is UUID`; reject nil UUID |
| UUID preservation | Preserve the supplied UUID object/value exactly after validation |
| UUID rejection | Reject UUID subclasses, strings, bytes, integers, `None`, and arbitrary objects |
| `attempt_number` | Exact `int`, not `bool`; positive |
| `LeaseDuration.value` | Exact `timedelta`; strictly positive; no production default or maximum |
| `RecoveryBatchSize.value` | Exact `int`, not `bool`; positive |
| `AttemptFailureCode.value` | Exact built-in `str`; full match `[a-z][a-z0-9_]{0,63}` |
| `analysis_version` | Exact built-in `str`; full match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}` |
| `attempt_limit` | Exact `int`, not `bool`; positive |
| `video_id` | Exact built-in `str`; nonempty; full match `^[A-Za-z0-9_-]+$`; ASCII letters/digits, underscore, and hyphen only; reject all whitespace and non-ASCII Unicode; preserve accepted case and spelling exactly |
| `player_id` | Exact built-in `str`; nonempty; full match `^[A-Za-z0-9_-]+$`; ASCII letters/digits, underscore, and hyphen only; reject all whitespace and non-ASCII Unicode; preserve accepted case and spelling exactly |
| `video_reference` | Exact built-in `str`; nonempty; must equal `value.strip()`; contain no NUL, `/`, or `\`; be neither a native absolute path nor an absolute/drive-qualified Windows path; contain no `..` path part; and equal `Path(value).name`; preserve accepted Unicode, case, and internal whitespace exactly |
| `canonicalize_callback_url` input | Accept exactly values satisfying `isinstance(value, HttpUrl)`; `HttpUrl` subclasses are accepted; every other input type receives the fixed sanitized canonicalization `TypeError` below |
| `callback_url` | Durable/domain value is an exact built-in nonempty `str`; parse it with the current Pydantic `HttpUrl` adapter, reject a fragment, compute `str(canonicalize_callback_url(validated_url))`, and require the stored text to equal that canonical string exactly; apply no other normalization; any Unicode, case, or whitespace spelling changed by parsing is rejected, while text already equal to the canonical output is preserved exactly |
| Datetimes | Timezone-aware `datetime` with a valid non-`None` offset; preserve aware non-UTC values |
| `WorkerClaim` | `heartbeat_at >= started_at`; `lease_expires_at > heartbeat_at` |
| `LeaseRenewed` | `lease_expires_at > heartbeat_at` |
| Recovery counts | Exact non-negative integers |
| Already-applied status | Only `RETRYABLE_FAILURE`, `TERMINAL_FAILURE`, or `EXHAUSTED` |

The analysis-version format admits the current `1.0.0` form while rejecting whitespace, control
characters, package metadata, commit messages, and diagnostic text.

The exact-type and non-nil rule applies to every UUID-valued public/domain field, including any
future value added to this contract; no UUID field may silently coerce another representation.

The failure-code pattern freezes representation only. It does not approve a vocabulary or permit
exception messages, exception class names, `repr`, traceback, SQL, URLs, or user-controlled text.

### Claim text validation ownership and failures

RED-F owns alignment of durable domain and admission validation with the worker-safe request rules.
It must apply the table above to `NewAnalysisJob`, `AnalysisJob`, and `ClaimableAnalysisJob`, and it
must update the admission contract so a newly admitted job cannot persist text that the child
request boundary would reject. The service's existing `callback_url: HttpUrl` input remains the
validated public/admission value. The canonicalization input predicate remains exactly
`isinstance(value, HttpUrl)`, so `HttpUrl` subclasses are accepted. Canonicalization rejects a
fragment and renders the canonical string. Separately, the stored/domain `callback_url` must be an
exact built-in nonempty `str` and must satisfy the canonical-string equality rule above. All four
durable values are validated before protected-key work, fingerprinting, or repository I/O.

The exact failure behavior is fixed and sanitized:

| Invalid input | Required failure |
|---|---|
| Any durable/domain text value is not an exact built-in `str`, or an admission identifier is not an exact built-in `str` | `TypeError("Admission identifiers must be strings.")` |
| Empty or otherwise unsafe `video_id` or `player_id` | `ValueError("child analysis identifiers must be safe")` |
| Empty or otherwise unsafe `video_reference` | `ValueError("video_reference must be a safe relative filename")` |
| Callback parsing fails or the supplied string differs from its canonical representation | `ValueError("Callback URL is invalid.")` |
| Callback canonicalization receives a value for which `isinstance(value, HttpUrl)` is false | `TypeError("Callback URL must be validated before canonicalization.")` |
| The validated callback URL contains a fragment | `ValueError("Callback URL must not contain a fragment.")` |

No failure message, exception graph, or log may include the rejected value. Validation does not
case-fold, trim, Unicode-normalize, rewrite path text, reorder query parameters, or otherwise repair
the durable text. For callback URLs only, the existing `HttpUrl` parser defines the canonical form;
admission stores the canonical string rendered from its already validated `HttpUrl`, and the
durable/domain validator requires that stored string to reproduce itself exactly. This does not
change the current public `AnalyzeRequest` schema or impose that equality check on raw JSON before
Pydantic validation.

RED-H independently reapplies these exact rules while decoding every database row and maps a
violation to the fixed sanitized persistence-invariant error. That defensive decode is not the
first enforcement boundary. Before RED-H or any activation, every environment must satisfy the
RED-G empty-table apply precondition: any existing Slice 2 rows must be explicitly exported and
removed, or handled by a separately approved compatibility migration. No legacy row may be assumed
worker-safe merely because Slice 2 accepted it.

Fingerprint V1 remains byte-for-byte unchanged: its field names, order, JSON serialization, strict
UTF-8 encoding, SHA-256 operation, and canonical callback input remain exactly as implemented.
RED-F validates worker-safe syntax before invoking that existing algorithm; it neither adds a
fingerprint field nor normalizes an accepted fingerprint component.

## Worker slot, lease, and fencing

`WorkerSlotId` is stable only for one execution-slot incarnation. It may be reused across several
sequential claims by that live slot. A recreated slot or restarted process must create a new UUID.

`LeaseId` is created once for each successful new claim. It is not reused across attempts and is
not derivable from process, host, thread, task, or slot identity.

The exact mutation-authority tuple is:

```text
(job_id, attempt_number, worker_slot_id, lease_id)
```

Every worker-owned mutation must additionally match:

- job state `RUNNING`;
- job `attempt_count == attempt_number`;
- the attempt primary key `(job_id, attempt_number)`;
- attempt status `ACTIVE`; and
- `lease_expires_at >` database current time.

The components are all required: job ID prevents cross-job authority, attempt number rejects an
older generation, slot ID binds the current execution-slot incarnation, and lease ID distinguishes
the particular claim.

### Same-slot claim retry

`claim_next` first examines active ownership for the supplied slot:

- If the slot has a current, unexpired, structurally valid active attempt, return the exact persisted
  `WorkerClaim`, including its existing lease and timestamps. Do not increment the attempt number.
- If the slot has an expired or otherwise non-returnable but structurally valid active attempt,
  return `WorkerSlotUnavailable`. Do not claim a second job.
- If the slot is free and no job is eligible, return `NoJobAvailable`.
- Contradictory or malformed persisted state raises the sanitized invariant exception; it is not
  converted to `WorkerSlotUnavailable`.

`ClaimLost` is never returned by `claim_next` because the caller supplied no authority. It remains
the ordinary concurrency outcome for heartbeat and failure operations carrying an authority tuple.

## Job and attempt lifecycle

The public durable job-state vocabulary remains exactly:

```text
QUEUED
RUNNING
COMPLETED
FAILED
CANCELLED
```

RED-F adds only `RUNNING -> QUEUED` to the durable domain transition graph.

| Event | Job transition | Attempt result |
|---|---|---|
| Fresh claim | `QUEUED -> RUNNING` | Insert `ACTIVE`; increment attempt count once |
| Valid same-slot claim retry | No transition | Return exact persisted claim; no new lease or attempt |
| Slot has non-returnable active claim | No transition | `WorkerSlotUnavailable`; recovery must resolve it |
| Heartbeat | `RUNNING -> RUNNING` | Remains `ACTIVE`; advance timestamps; fence unchanged |
| Retryable failure below limit | `RUNNING -> QUEUED` | `RETRYABLE_FAILURE` |
| Retryable failure at limit | `RUNNING -> FAILED` | `EXHAUSTED` |
| Terminal classified failure | `RUNNING -> FAILED` | `TERMINAL_FAILURE` |
| Lease expiry below limit | `RUNNING -> QUEUED` | `INTERRUPTED` |
| Lease expiry at limit | `RUNNING -> FAILED` | `EXHAUSTED` |
| Unexpected worker error/crash | No worker finalization | Lease expiry and recovery resolve it |
| Successful completion | Deferred to Slice 4 | Requires durable result/outbox transaction |
| Future cancellation wins | `RUNNING -> CANCELLED` | Active authority invalidated; Slice 6 owns policy |

`analysis_version` and `attempt_limit` remain unchanged across every attempt.

## Unknown exceptions and classified failures

Only explicitly recognized, typed failures may call a failure finalizer:

- a known retryable failure calls `finalize_retryable_failure`;
- a known terminal failure calls `finalize_terminal_failure`;
- an unexpected programming error propagates unchanged;
- `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit`, `MemoryError`, and fatal
  `BaseException` preserve identity;
- an unexpected failure stores no arbitrary failure text and receives no guessed business code;
- an unexpected worker disappearance leaves the active lease to expire; and
- recovery records `INTERRUPTED`, or `EXHAUSTED` if the immutable attempt limit has been reached.

Operational alerting for an unknown crash is required before activation, but it cannot change this
durable classification. The current broad processor behavior that turns arbitrary `Exception` into
`FAILED` must not be carried into the durable coordinator.

The public worker persistence exceptions are exactly:

```python
class AnalysisJobWorkerPersistenceUnavailable(RuntimeError):
    """Expected worker persistence operation is unavailable."""


class AnalysisJobWorkerPersistenceInvariantError(RuntimeError):
    """Stored worker/job data violates the durable contract."""
```

Pool, connection, statement, and lock timeouts map to the fixed sanitized unavailable error.
Malformed rows and impossible durable state map to the fixed sanitized invariant error. Unexpected
programming and fatal errors propagate unchanged. Ordinary cleanup failure must not replace an
active primary failure.

## Retry, exhaustion, and cancellation

- `attempt_limit` is selected explicitly at admission and is immutable.
- `attempt_count` begins at zero.
- A successful new claim increments it exactly once.
- Same-slot response-loss recovery, heartbeat, and recovery do not increment it.
- A new post-recovery claim increments it.
- A claim may not make `attempt_count > attempt_limit`.
- Retryable failure or expiry below the limit requeues the same job.
- Retryable failure or expiry at the limit transitions the job to internal state `FAILED` and the
  attempt to `EXHAUSTED`.
- A stored `attempt_count > attempt_limit` is a persistence invariant violation.
- The externally visible Slice 4 failure mapping for exhaustion remains a later product decision.

Slice 3 exposes no cancellation operation or API. Future Slice 6 must ensure committed `CANCELLED`
jobs are never claimed, cancellation invalidates active authority, stale owners return `ClaimLost`,
and shutdown is not automatically business cancellation. Authentication, callback behavior, and
product-visible races remain Slice 6 policy.

## Database-authoritative time

- Every domain timestamp is timezone-aware.
- Persistence decisions use the PostgreSQL clock.
- Callers supply `LeaseDuration`, not an absolute expiry.
- New expiry is calculated from current database time, not the previous expiry or a process clock.
- No minimum, maximum, lease duration, heartbeat cadence, or polling cadence is selected here.
- Deterministic tests use controlled database timestamps, locks, barriers, and bounded outer
  timeouts rather than wall-clock sleeps.
- If ownership cannot be confirmed before the last confirmed expiry, authoritative processing
  stops and any later mutation receives `ClaimLost`.

## RED-G schema and migration contract

Create one reversible migration depending on `sprint2_slice2_analysis_job`. Slice 1 and Slice 2
migration bytes remain unchanged.

### Apply precondition

The migration must refuse whenever `analysis_jobs` contains any row, regardless of its state.
There is no attempt-limit or analysis-version backfill. Production preflight must prove the still
inactive table is empty. A nonempty environment requires explicit operator cleanup/export or a
separately approved migration plan.

### `analysis_jobs` additions

```text
analysis_version TEXT NOT NULL
attempt_limit INTEGER NOT NULL CHECK (attempt_limit > 0)
attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0)
CHECK (attempt_count <= attempt_limit)
```

`analysis_version` must satisfy `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. There is no default for
`analysis_version` or `attempt_limit`.

### `analysis_job_attempts`

```text
job_id UUID NOT NULL
attempt_number INTEGER NOT NULL CHECK (attempt_number > 0)
worker_slot_id UUID NOT NULL
lease_id UUID NOT NULL
status TEXT NOT NULL
started_at TIMESTAMPTZ NOT NULL
heartbeat_at TIMESTAMPTZ NOT NULL
lease_expires_at TIMESTAMPTZ NULL
finished_at TIMESTAMPTZ NULL
safe_failure_code TEXT NULL
PRIMARY KEY (job_id, attempt_number)
FOREIGN KEY (job_id) REFERENCES analysis_jobs(job_id) ON DELETE RESTRICT
```

Required indexes permit at most one `ACTIVE` attempt per job, at most one per worker slot, and add a
partial active-expiry index on `(lease_expires_at, job_id, attempt_number)`.

| Status | Required shape |
|---|---|
| `ACTIVE` | `started_at IS NOT NULL`; `heartbeat_at IS NOT NULL`; `finished_at IS NULL`; `safe_failure_code IS NULL`; `heartbeat_at >= started_at`; `lease_expires_at IS NOT NULL`; `lease_expires_at > heartbeat_at` |
| `INTERRUPTED` | `started_at IS NOT NULL`; `heartbeat_at IS NOT NULL`; `finished_at IS NOT NULL`; `heartbeat_at >= started_at`; `finished_at >= heartbeat_at`; `lease_expires_at IS NULL`; `safe_failure_code IS NULL` |
| `RETRYABLE_FAILURE` | `started_at IS NOT NULL`; `heartbeat_at IS NOT NULL`; `finished_at IS NOT NULL`; `heartbeat_at >= started_at`; `finished_at >= heartbeat_at`; `lease_expires_at IS NULL`; bounded `safe_failure_code IS NOT NULL` |
| `TERMINAL_FAILURE` | `started_at IS NOT NULL`; `heartbeat_at IS NOT NULL`; `finished_at IS NOT NULL`; `heartbeat_at >= started_at`; `finished_at >= heartbeat_at`; `lease_expires_at IS NULL`; bounded `safe_failure_code IS NOT NULL` |
| `EXHAUSTED` after classified failure | `started_at IS NOT NULL`; `heartbeat_at IS NOT NULL`; `finished_at IS NOT NULL`; `heartbeat_at >= started_at`; `finished_at >= heartbeat_at`; `lease_expires_at IS NULL`; bounded `safe_failure_code IS NOT NULL` |
| `EXHAUSTED` after expiry | `started_at IS NOT NULL`; `heartbeat_at IS NOT NULL`; `finished_at IS NOT NULL`; `heartbeat_at >= started_at`; `finished_at >= heartbeat_at`; `lease_expires_at IS NULL`; `safe_failure_code IS NULL` |

Any non-null failure code must satisfy `[a-z][a-z0-9_]{0,63}`.

RED-G enforces every nullability, failure-code, and timestamp-ordering shape above with PostgreSQL
`CHECK` constraints; the column-level `NOT NULL` declarations for `started_at` and `heartbeat_at`
remain required. RED-H independently revalidates the same ordering and status shape while decoding
database rows. Any row with `heartbeat_at < started_at`, any resolved row with
`finished_at < heartbeat_at`, or any other shape violation is a persistence invariant failure and
must not be exposed as a valid domain attempt.

### Downgrade precondition

Downgrade must refuse if `analysis_jobs` contains any row, `analysis_job_attempts` contains any row,
or any Slice 3-owned durable state exists. `attempt_count == 0` is not sufficient: downgrade would
still discard immutable analysis version and attempt limit values.

## RED-H transaction obligations

### Slice 2 admission compatibility

The Slice 2 select/insert/returning/decode paths add `analysis_version`, `attempt_limit`, and
`attempt_count`. New admission persists the explicit version and limit and starts attempt count at
zero. Existing lookup remains first: identical lookup/fingerprint returns the stored job even if a
new proposed version or limit differs; the stored values do not change. A different fingerprint
remains an idempotency conflict.

### Claim transaction

One short transaction must:

1. Apply bounded local statement/lock settings.
2. Check active ownership for the supplied worker slot.
3. Return an exact valid, unexpired same-slot claim when present.
4. Return `WorkerSlotUnavailable` for an expired/non-returnable structurally valid active claim.
5. Never claim a second job for that slot.
6. Select one `QUEUED` job with `attempt_count < attempt_limit`, ordered by accepted time and job ID,
   using `FOR UPDATE SKIP LOCKED`.
7. Generate one opaque lease UUID through an injected/testable factory.
8. Increment attempt count exactly once, set `RUNNING`, and insert the active attempt using database
   timestamps.
9. Return the complete execution claim.
10. Commit and release every database resource before the result escapes.

Heartbeat matches the complete authority tuple, current generation, `RUNNING`, `ACTIVE`, and an
unexpired lease, then advances heartbeat and expiry from database time without incrementing the
fence. Zero matching rows return `ClaimLost`.

Retryable failure below the limit records `RETRYABLE_FAILURE` and requeues. Retryable failure at the
limit records `EXHAUSTED` and fails. Terminal classified failure records `TERMINAL_FAILURE` and
fails. Exact replay returns `AttemptResolutionAlreadyApplied`; a conflicting or stale request
returns `ClaimLost`.

Recovery receives an explicit batch size and issues one matching SQL `LIMIT`. It uses database time
and `FOR UPDATE SKIP LOCKED`, resolves only current expired active attempts, and returns counts only
after commit and resource release. The repository supplies no batch default or hidden maximum.

## RED-I disposable-PostgreSQL scenarios

1. Distinct slots claim distinct jobs; two slots racing for one job produce one owner.
2. Several slot UUIDs in one simulated process operate independently.
3. One slot cannot own two active attempts.
4. Same-slot response-loss retry returns the exact persisted job, lease, policy, and timestamps.
5. Expired same-slot claim returns `WorkerSlotUnavailable` without a lease identity.
6. A recreated slot UUID cannot impersonate its predecessor.
7. Forced expiry, recovery, and reclaim produce generation two with unchanged policy snapshots.
8. Heartbeat advances timestamps without changing generation; future expiry is not recovered.
9. Stale heartbeat/failure returns `ClaimLost` and changes no authoritative row.
10. Retryable, terminal, interrupted, and exhausted paths produce their exact transitions/statuses.
11. Unknown programming failure writes no resolution; expiry recovery records interruption or
    exhaustion without exception text.
12. Cancelled and other terminal jobs are ignored by claim/recovery.
13. Claim frees queued capacity; recovery may exceed it; later admission still counts only queued.
14. Same lookup/fingerprint with changed proposed policy returns the stored policy; changed
    fingerprint conflicts.
15. Committed claims release locks/connections immediately; failed transactions roll back both job
    and attempt mutations.
16. Pool/statement/lock failures are sanitized and runtime roles remain least privilege.
17. Apply and downgrade refuse all nonempty durable-table cases; empty apply/rollback/reapply is
    deterministic.
18. Races use barriers, locks, forced timestamps, and bounded outer timeouts, never arbitrary sleeps.
19. Disposable resources and records are removed completely.
20. The required real-PostgreSQL lease-boundary race below proves recovery wins over one blocked
    authority-bearing mutation and that the old owner stays fenced after reclaim.

### Deterministic lease-boundary recovery race

RED-I must execute this scenario against disposable real PostgreSQL; an in-memory repository,
mocked lock, or in-memory concurrency simulation is not acceptable. The scenario uses an attempt
below its immutable limit so recovery records `INTERRUPTED` and a later reclaim is permitted. The
authority-bearing mutation selected for the race is either lease renewal or retryable-failure
finalization, and the evidence must identify which operation was exercised.

1. Persist one `RUNNING` job with one `ACTIVE` attempt whose database timestamp makes its lease
   expired and whose authority is the complete old tuple
   `(job_id, attempt_number, worker_slot_id, lease_id)`.
2. Open separate real PostgreSQL connections and transactions for recovery and the old owner's
   authority-bearing mutation.
3. Use explicit test barriers plus PostgreSQL row locks to prove the recovery transaction has
   obtained and is holding the authoritative job/attempt lock before it is allowed to commit. Do
   not infer lock ownership from elapsed time or a sleep.
4. While recovery holds that lock, start the selected mutation with the old authority tuple and use
   deterministic coordination plus a bounded outer timeout to prove it is blocked on database
   authority rather than completed.
5. Permit recovery to commit exactly one `ACTIVE -> INTERRUPTED` attempt transition and the matching
   `RUNNING -> QUEUED` job transition.
6. Release the blocked mutation by that commit and await its result.
7. Require the old-owner mutation to return `ClaimLost` and prove it changed zero job or attempt
   rows.
8. Query authoritative rows and prove exactly one transition committed: the job is `QUEUED`, the
   old attempt retains its original attempt number and complete fencing values, its status is
   `INTERRUPTED`, and no competing resolution exists.
9. Prove active-attempt cardinality for both the job and the old worker slot is zero, with no second
   `ACTIVE` attempt present before reclaim.
10. Claim once with a new worker-slot UUID and prove the job is `RUNNING` with exactly one new
    `ACTIVE` attempt: the same `job_id`, `attempt_number == old attempt_number + 1`, a new
    `worker_slot_id`, and a new `lease_id`. Query both attempt rows and assert active cardinality is
    exactly one.
11. After reclaim, call both heartbeat and a failure finalizer with the complete old authority
    tuple. Each must return `ClaimLost`, change zero rows, leave the new fencing values unchanged,
    and leave exactly one `ACTIVE` attempt owned only by the new authority.

The retained RED-I evidence must assert, from database reads, the job state, every involved attempt
status and attempt number, all four fencing-tuple values, and per-job/per-slot active-attempt
cardinality at the post-recovery, pre-reclaim, and post-reclaim boundaries. Full lease UUIDs are
test assertions only and must not be emitted to operational logs.

## Slice 2 admission and fingerprint compatibility

```python
async def admit_analysis_job(
    *,
    repository: AnalysisJobAdmissionRepository,
    protector: IdempotencyLookupProtector,
    job_id: UUID,
    caller_scope: str,
    raw_idempotency_key: str,
    video_id: str,
    player_id: str,
    video_reference: str,
    callback_url: HttpUrl,
    analysis_version: str,
    attempt_limit: int,
    max_queue_size: int,
) -> AnalysisJobAdmissionOutcome: ...
```

Analysis version and attempt limit are required internal policy inputs. They are not current HTTP
fields and have no defaults. Neither participates in fingerprint V1, so existing fingerprint
preimages, digest vectors, and HMAC protected-lookup framing remain unchanged. Same-key identical
request retries retain stored policy snapshots; a different request fingerprint remains a conflict.

## Security and observability restrictions

Safe operational fields are limited to job ID, attempt number/status, attempt limit, validated
bounded analysis version, worker-slot ID, bounded counts/durations, and operation/outcome names.

Ownership/security logs must omit callback URL, video reference, idempotency material, fingerprint,
HMAC state, full lease UUID, SQL/parameters, database identity/credentials, raw exception data,
payloads, and runtime-resource handles. Use `worker_slot_id`, not a generic process-oriented
`worker_id`. Callback URL is execution input, not an ownership credential.

## Configuration and activation blockers

Bounded recovery means every invocation receives an explicit positive batch size and issues one SQL
`LIMIT`. Repository code supplies no default. Later configuration must enforce an
Operations-approved production cap.

Activation is blocked until Operations approves measured values for attempt limit, analysis
version, lease duration, heartbeat cadence, recovery batch cap, and polling cadence, and assigns a
named operational owner. No source-code, repository, or HTTP default may replace these approvals.
RED-F/G/H/I completion alone cannot authorize a worker canary. Until every applicable later slice
and the explicit Slice 11 cutover authorization are complete, all worker modules and adapters must
remain inactive and uncomposed, with no production claim loop.

## Deployment ordering

1. Keep the durable application and worker path inactive.
2. Deliver backward-compatible schema and code dark, with worker modules/adapters uncomposed and no
   claim loop running.
3. Prove production `analysis_jobs` is empty.
4. Apply the Slice 3 migration only through the separately authorized migration operation.
5. Verify schema, least-privilege roles, migration evidence, and rollback refusal behavior.
6. Provision and verify approved runtime secrets, production roles/grants, and the approved attempt,
   version, lease, heartbeat, recovery, and polling values without composing the worker.
7. Complete and approve RED-I disposable-PostgreSQL integration evidence; it does not authorize a
   production smoke claim or canary.
8. Implement and verify Slice 4 successful fenced finalization, durable result persistence, and
   transactional callback-outbox behavior before any production worker claim.
9. Complete every later prerequisite slice applicable to the production path, including the Slice
   10 disposable-database crash matrix and required Apex/infrastructure conformance evidence.
10. Complete runtime wiring, observability, rollback readiness, production migration/cutover
    rehearsal, and deployment approval while the claim loop remains disabled.
11. Record the explicit human authorization required by the separately reviewed Slice 11 cutover
    gate; RED-F/G/H/I or Slice 4 completion is not a substitute.
12. Only after that Slice 11 authorization may the activation commit compose the worker and start
    exactly one worker slot as a production canary.
13. Increase slots only after measurement and separate approval.

The migration must not break the active in-memory admission path. If schema and code cannot be
independently backward-compatible, use a bounded maintenance window while the durable path remains
inactive. No worker canary, including a one-slot canary, may claim production jobs before Slice 4,
all applicable later prerequisites, and Slice 11 authorization are complete.

## Remaining decisions

These do not block inactive RED-F but block their applicable later boundary:

| Decision | Blocking boundary |
|---|---|
| Numeric production attempt limit | Runtime composition and activation |
| Concrete production analysis-version value | Runtime composition and activation |
| Lease duration | Coordinator and activation |
| Heartbeat cadence | Coordinator and activation |
| Recovery batch cap | Coordinator and activation |
| Polling cadence | Coordinator and activation |
| Named operational owner | Operational approval and activation |
| Slice 4 external exhaustion/failure mapping | Result/outbox and external callback contract |
| Slice 6 cancellation authority and product policy | Durable cancellation and activation |

No numeric default is selected by this document.

## RED-F acceptance criteria

RED-F must prove exact modules/exports, status vocabulary, fields/order/types, immutable slotted
values, strict UUID validation, complete claim payload, exclusion of sensitive/runtime state,
version/failure-code formats, slot/lease separation, the full fence, exact keyword-only operations,
claim versus mutation outcome separation, zero-I/O validation, one repository call, exception
identity preservation, and `RUNNING -> QUEUED` without another public job state.

RED-F must not add a success finalizer, cancellation API, public schema, runtime composition,
process count, HMAC change, configuration, active queue change, migration, SQL, Psycopg adapter,
Docker/PostgreSQL action, deployment, or activation.

## Rejected alternatives

- Per-process ownership, or PID/host/thread/task-derived identity.
- Reusing slot UUID as lease UUID.
- Holding row, session, or advisory locks throughout analysis.
- State-only, attempt-only, slot-only, or lease-only fencing.
- Incrementing the fence on heartbeat.
- Returning `ClaimLost` from `claim_next` or granting a second claim to an unavailable slot.
- Repository inspection of exception classes or conversion of unknown errors to terminal failures.
- Free-form failure statuses, Boolean dispositions, or a public `RETRYING` job state.
- Including analysis version or attempt limit in fingerprint V1.
- Recomputing analysis version during retry or supplying silent policy/timing defaults.
- Backfilling nonempty jobs or downgrading while any job remains.
- Omitting callback URL and forcing a second unfenced lookup.
- Logging callback URLs, lease authority, or raw failure data.
- Defining successful finalization before Slice 4 or cancellation before Slice 6.
- Adding a sweeper, broker, Redis, process, or worker in Slice 3.

## Related decisions and contracts

- [ADR-004: Analysis job lifecycle and idempotency](../../decisions/ADR-004-analysis-job-lifecycle-and-idempotency.md)
- [ADR-005: Durable job storage and worker architecture](../../decisions/ADR-005-durable-job-storage-and-worker-architecture.md)
- [ADR-008: Durable job recovery and callback outbox](../../decisions/ADR-008-durable-job-recovery-and-callback-outbox.md)
- [Analysis Job Contract V1](../../contracts/analysis-job-contract-v1.md)
- [Sprint 2 implementation plan](03-implementation-plan.md)
