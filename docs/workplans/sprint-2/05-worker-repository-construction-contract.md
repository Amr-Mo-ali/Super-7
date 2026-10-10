# PLANNING-H — Concrete Worker Repository Construction Boundary

> Status: Approved construction decisions; documentation awaiting independent review
> Authorization: Documentation only; no RED-H tests or GREEN-H implementation
> Runtime activation: Not approved
> Base commit: `3dabaa5caf0c69487e47b6c691d9f0294f5a8793`
> Branch: `feat/sprint-2-worker-claim-lease-fencing`

## 1. Summary and exact declarations

This contract records the approved proposal and supplements [Planning-F](04-worker-claim-lease-fencing-contract.md) only at the concrete construction boundary. It preserves the five Worker operations, outcome unions, fencing, transaction obligations, policy snapshots and runtime inactivity.

Approved choices: required factory injection, one shared Worker lock timeout, and no pool probing during construction. These are specification decisions, not authorization to change tests or implementation. The declaration bodies below are specification placeholders, not executable implementation.

Module: `adapters.psycopg_analysis_job_worker_repository`.

```python
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID

from services.analysis_job_worker import (
    AnalysisJobWorkerPersistenceInvariantError,
    AnalysisJobWorkerPersistenceUnavailable,
)

__all__ = [
    "PsycopgAnalysisJobWorkerRepository",
    "AnalysisJobWorkerPersistenceUnavailable",
    "AnalysisJobWorkerPersistenceInvariantError",
]


class _Cursor(Protocol):
    async def fetchone(self) -> tuple[object, ...] | None: ...

    async def fetchall(self) -> list[tuple[object, ...]]: ...

    async def close(self) -> None: ...


class _Connection(Protocol):
    def transaction(self) -> AbstractAsyncContextManager[object]: ...

    async def execute(
        self,
        query: str,
        params: tuple[object, ...] | None = None,
    ) -> _Cursor: ...


class _Pool(Protocol):
    def connection(
        self,
        *,
        timeout_seconds: float,
    ) -> AbstractAsyncContextManager[_Connection]: ...


class PsycopgAnalysisJobWorkerRepository:
    def __init__(
        self,
        *,
        pool: _Pool,
        connection_acquire_timeout_seconds: float,
        operation_timeout_ms: int,
        lock_timeout_ms: int,
        lease_id_factory: Callable[[], UUID],
    ) -> None: ...
```

All constructor arguments are required and keyword-only, including `pool`. This deliberately differs from the existing admission constructor’s positional pool argument; its other applicable timeout conventions are retained.

The concrete class must implement these signatures unchanged:

```python
async def claim_next(
    self, *, worker_slot_id: WorkerSlotId, lease_duration: LeaseDuration
) -> WorkerClaim | NoJobAvailable | WorkerSlotUnavailable: ...

async def renew_lease(
    self, *, authority: AttemptAuthority, lease_duration: LeaseDuration
) -> LeaseRenewed | ClaimLost: ...

async def finalize_retryable_failure(
    self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
) -> (
    AttemptRequeued | AttemptExhausted | AttemptResolutionAlreadyApplied | ClaimLost
): ...

async def finalize_terminal_failure(
    self, *, authority: AttemptAuthority, failure_code: AttemptFailureCode
) -> AttemptFailed | AttemptResolutionAlreadyApplied | ClaimLost: ...

async def recover_expired(
    self, *, batch_size: RecoveryBatchSize
) -> RecoverySummary: ...
```

The error exports are aliases to the existing service classes, to be verified by identity—not subclasses or duplicate definitions. No Worker-specific lock-timeout exception is introduced. Structural protocols, SQL, decoders and factory helpers remain private.

Construction neither inspects nor invokes the pool. `PsycopgDatabasePool` compatibility must be established through a real typed composition witness without casting away incompatibilities. A miswired pool is a programming defect, not an operational persistence failure.

## 2. Timeout validation, transaction settings and errors

Validation runs in this order: acquisition timeout; statement timeout; lock timeout; timeout ordering; factory configuration. It completes before any pool interaction or factory invocation.

| Input/rule | Accepted boundary | Fixed failure |
|---|---|---|
| Acquisition type | `isinstance(value, (int, float))`, excluding `bool`; numeric subclasses accepted as in admission | `TypeError("Connection acquisition timeout must be numeric.")` |
| Acquisition value | Positive, finite and convertible to a positive finite `float`; normalize once to that float | `ValueError("Connection acquisition timeout must be finite and positive.")` |
| Either millisecond timeout type | `type(value) is int`; reject bool and subclasses | `TypeError("PostgreSQL millisecond timeout must be an integer.")` |
| Either millisecond timeout range | `1 <= value <= 2_147_483_647` | `ValueError("PostgreSQL millisecond timeout is outside the approved range.")` |
| Ordering | `operation_timeout_ms > lock_timeout_ms` | `ValueError("Operation timeout must exceed lock timeout.")` |
| Factory callable | `callable(lease_id_factory)` | `TypeError("Lease UUID factory must be callable.")` |
| Known asynchronous factory | Reject coroutine/async-generator functions and callable objects whose `__call__` has either form | `TypeError("Lease UUID factory must be synchronous.")` |

Ordinary numeric conversion overflow produces the fixed acquisition-value failure outside its handler. There is no additional business maximum or relationship between acquisition timeout, lease duration and statement timeout.

Every Worker operation:

1. Validates public inputs before pool interaction.
2. Obtains one connection using `connection(timeout_seconds=...)`.
3. Enters one short transaction.
4. Executes and closes these setting cursors, in order:

```text
SQL: SELECT set_config(%s, %s, %s)

("statement_timeout", str(operation_timeout_ms), True)
("lock_timeout", str(lock_timeout_ms), True)
```

5. Performs the approved operation.
6. Commits and releases cursors, transaction and connection before exposing its outcome.

Both settings precede durable-table statements. One `lock_timeout_ms` applies to all five operations; there are no operation-specific overrides, advisory locks, session settings or independent commits.

`operation_timeout_ms` retains admission’s name but bounds each SQL statement—not the entire operation. Lock timeout bounds individual lock waits; transaction-local settings expire with the transaction. No hard total-operation deadline is claimed. [PostgreSQL timeout semantics](https://www.postgresql.org/docs/current/runtime-config-client.html), [transaction-local configuration](https://www.postgresql.org/docs/current/functions-admin.html).

| Failure boundary | Required behavior |
|---|---|
| Actual pool/connection/transaction/cursor I/O raises `PoolTimeout`, `OperationalError` or `InterfaceError` | Existing Worker unavailable class, message below |
| Driver `QueryCanceled` or `LockNotAvailable` | Same unavailable outcome; installed Psycopg defines both as `OperationalError` |
| Malformed persisted rows or impossible durable state | Existing Worker invariant class, message below |
| Programming exception, including a non-driver object with a timeout-looking `sqlstate` | Propagate unchanged |
| Factory exception, even if its class resembles a driver failure | Propagate unchanged; factory execution is not a persistence-I/O boundary |
| Python cancellation, `MemoryError` or fatal `BaseException` | Never translate; preserve identity |
| Ordinary cleanup failure with an active primary failure | Preserve the primary |
| Fatal cleanup failure | Propagate it by identity, retaining the already-sanitized primary as cause |
| Cleanup failure without a primary | Apply the same narrow boundary classification; no successful outcome escapes |

Approved exact Worker messages:

```text
PostgreSQL AnalysisJob worker persistence is unavailable.
PostgreSQL AnalysisJob worker persistence invariant failed.
```

Translated public errors have exactly that single message argument, no added attributes, and no retained raw cause/context. Raise them outside the driver/domain-validation handler. Do not classify by exception text or arbitrary SQLSTATE attributes, and do not log rejected data, SQL, parameters or driver diagnostics.

## 3. Lease UUID factory contract

`lease_id_factory: Callable[[], UUID]` is synchronous, takes no arguments and has no default. Construction never invokes it or probes its arity by calling it.

Invoke it exactly once after a fresh eligible job has been selected and locked, but before incrementing its generation or writing job/attempt mutations.

| Situation | Factory calls |
|---|---:|
| Fresh claim reaches lease generation | 1 |
| Valid same-slot claim retry | 0 |
| `WorkerSlotUnavailable` or `NoJobAvailable` | 0 |
| Rejected input, malformed selected state or failure before lease generation | 0 |
| Renewal, either failure finalizer or recovery | 0 |
| Failure after generation | Remains 1; no internal retry |

Return validation:

- Require `type(value) is UUID`; reject strings, bytes, integers, wrappers, awaitables and UUID subclasses.
- Reject the nil UUID.
- Preserve the returned UUID object/value exactly when wrapping it as `LeaseId`.
- Do not impose a UUID-version restriction.
- Wrong return type: `TypeError("Lease UUID factory must return an exact UUID.")`.
- Nil return: `ValueError("Lease UUID factory must not return a nil UUID.")`.

A synchronous wrapper returning a native coroutine is invalid: close that unstarted coroutine, do not await it, then raise the fixed type failure. Other invalid objects are neither awaited nor operated on.

Wrong arity and exceptions raised by the factory propagate unchanged, with transaction rollback and cleanup. Generated-but-uncommitted UUIDs are discarded; a later caller retry follows Planning-F’s persisted same-slot lookup first.

The factory supplies a fresh opaque UUID for each new claim. The adapter does not add a uniqueness index, global UUID-history cache or extra inequality against worker-slot identity. Existing authorities are never regenerated during retry, renewal, finalization or recovery.

## 4. Narrow test evolution and snapshot separation

Use explicit test-fixture values such as `"1.0.0"` and `3`; these are not production defaults. Both snapshots remain required, validated before protector calls, fingerprinting and repository I/O.

| Representation | Snapshot/count fields |
|---|---|
| `NewAnalysisJob` | Required `analysis_version: str`, `attempt_limit: int`; no `attempt_count` |
| `AnalysisJob` | Required version and limit in Planning-F’s exact field order; no `attempt_count` |
| `ClaimableAnalysisJob` | Existing approved version and limit fields unchanged |
| Private persistence decoding | Version, limit and `attempt_count`; validate exact integer count with `0 <= count <= limit` |

For the existing admission SQL fixtures, preserve the original nine-column projection order and append:

```text
analysis_version, attempt_limit, attempt_count
```

Fresh insert binds the supplied version, supplied limit and integer `0`; accepted time remains database-generated. The appended parameter sequence is:

```text
candidate.analysis_version, candidate.attempt_limit, 0
```

Required bounded updates:

- **`tests/test_analysis_job_repository_contract.py`:** add snapshots to candidate/expected-job fixtures; add all three persistence values to row fixtures, SELECT/RETURNING projections and insert expectations. Update the directly shared SQL characterizations consistently. Preserve exact checks rather than replacing them with membership checks.
- **`tests/test_analysis_job_worker_contract.py`:** add explicit snapshots to `_durable_value` and `_admit`. Evolve the two pre-RED-H shape nodes to require Planning-F’s exact fields, annotations and constructor order, while retaining frozen/slotted assertions.
- **`tests/test_analysis_job_contract.py`:** update `_job`, `_candidate` and `_run_admission`; insert the two required keyword-only policy arguments before `max_queue_size` in the exact admission-signature assertion.

Identical lookup/fingerprint retries return authoritative stored policy even when the proposed version/limit differs. They neither overwrite policy nor become fingerprint conflicts. Fresh inserts must reject inconsistent returned policy/count data.

Fingerprint V1 field order, serialization and vectors, and HMAC framing remain byte-for-byte unchanged. No snapshot participates in either algorithm. Prior lifecycle, capacity, idempotency, cleanup, exception and validation assertions remain intact.

A later, separately authorized fixture-only update to `tests/test_postgres_analysis_job_repository_integration.py` is required: its direct `NewAnalysisJob` construction also needs explicit snapshots for GREEN-H strict typing and constructor compatibility. Do not change integration behavior or skips; this document does not authorize that later update.

## 5. RED scenarios, approval and deferred evidence

The new deterministic contract belongs at `tests/test_analysis_job_worker_repository_contract.py`. RED-H must characterize:

- Exact exports/error identity, complete resolved annotations, five unchanged operations and real-pool structural composition.
- Required keyword-only arguments; numeric/subclass boundaries; zero, negative, nonfinite, overflowing and maximum timeouts; equal/reversed ordering; zero pool/factory interaction during validation.
- Bound transaction-local settings on every operation, settings-cursor closure, no advisory lock and no outcome escaping failed commit/release.
- Factory call counts, strict UUID preservation, nil/subclass/async rejection, wrong arity, unchanged exception propagation and rollback after generation failure.
- Same-slot authority preservation; fresh generation increments; complete four-part mutation authority; ClaimLost lease-boundary behavior.
- Recovery limits/counts, retry/terminal/interruption/exhaustion outcomes, defensive persisted-row validation and absolute timestamp ordering.
- Snapshot persistence and validation, authoritative retry policy, unchanged fingerprint/HMAC evidence, and runtime inactivity.

Only actual absence of the intended adapter module maps to this approved fixed RED message:

```text
Slice 3 RED-H: adapters.psycopg_analysis_job_worker_repository is missing; GREEN-H must implement the approved worker persistence adapter.
```

Check discovery first; when the module exists, import it without masking transitive imports or programming defects. Classify snapshot absence separately. Future verification must enumerate actual node IDs and intentional failures, exclude only those exact nodes from the baseline, and report static findings without blanket suppression.

No construction decision remains unresolved in the approved proposal. This documentation is subject to independent review; RED-H tests, existing-test evolution and GREEN-H implementation require separate task authorization. Production numeric values, runtime composition and activation remain separate gates.

Scripted tests establish boundary behavior, not PostgreSQL concurrency. Retain future live proof of ownership/recovery races and downgrade waiting for an in-flight writer, refusing after commit, and preserving its row and policy/count data.

## 6. Independent review qualifications and source identity

The independent construction and snapshot-evolution review concluded `SAFE TO DOCUMENT PLANNING-H`.
That conclusion authorizes recording the approved boundary only. It does not authorize implementation,
test changes, integration execution, Git staging/commit/push, deployment or activation.

- Coroutine cleanup applies only to an actual unstarted native coroutine. Do not close a started
  coroutine or invoke `.close()`, `.aclose()`, or any other user-object cleanup hook on arbitrary
  awaitables or invalid objects. Do not await an invalid factory result.
- Values such as `analysis_version="1.0.0"` and `attempt_limit=3` are fixture examples, not production
  defaults. Required policy inputs and operator/runtime approval remain unchanged.
- The integration file's direct `NewAnalysisJob` construction is in `_candidate`. The smallest later
  fixture-only alignment is adding those two explicit policy arguments; no skip or integration
  behavior change is included.
- That fixture alignment alone does not restore live integration conformance. The existing integration
  file independently freezes a two-migration catalog and Slice 2 schema/version expectations in
  `_CATALOG` and `_prepare_database`. This pre-existing catalog/schema drift requires separate
  reconciliation before new live evidence can be claimed. It is not silently reconciled by this
  construction contract.
- The planning review established pool compatibility by source inspection. No Mypy composition
  witness was executed in that review; the real typed witness remains required future verification.
- PostgreSQL concurrency, ownership/recovery races and the writer-blocking downgrade scenario remain
  deferred live evidence. Scripted boundary tests cannot establish those results.
- Runtime remains inactive and uncomposed. Planning-F's applicable later slices, Slice 10 evidence
  and explicit Slice 11 authorization remain mandatory activation gates.

The complete input proposal was read and its supplied identity verified before documentation:

| Input artifact | Lines | Bytes | SHA-256 |
|---|---:|---:|---|
| `planning-h-worker-repository-construction-contract.md` | 244 | 14,820 | `50c17d28bb30501b2b53e8d645237aa5f6799806446e106d5c3893a21da8bb0f` |

The saved document's identity is reported separately in the documentation handoff; it differs from the
input because approval-boundary wording and the independent review qualifications are recorded here.
The approved declaration blocks, constructor, validation/error tables and RED scenarios are preserved.
