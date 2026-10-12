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
| Known asynchronous factory | Reject asynchronous behavior recognized by the approved FR2–FR7/FR10 rules in [§7](#7-approved-constructor-time-factory-recognition), after callability validation | `TypeError("Lease UUID factory must be synchronous.")` |

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

## 7. Approved constructor-time factory recognition

Decision date: 2026-10-12. The owner explicitly approved FR1–FR11 from the designated v2 proposal.
This section is the normative amendment. [The decision record](07-worker-factory-recognition-decision.md)
records rationale, considered alternatives and evidence; it does not add contract options.

Option A and import-time bootstrap are adopted. The intended later implementation is one correction,
reviewed in two logical parts. Implementation, test changes, commit approval and runtime activation
are not authorized by this documentation.

### FR1 — Retained construction obligations

The exact declarations and error aliases in [§1](#1-summary-and-exact-declarations), timeout types,
ranges and errors in [§2](#2-timeout-validation-transaction-settings-and-errors), and zero pool/factory
interaction remain unchanged. After acquisition, statement, lock and ordering validation, check
factory callability, then known-asynchronous behavior under the rules below. Successful import has
already completed FR11; constructors perform no bootstrap.

Acceptance establishes neither valid arity, synchronous execution nor a valid UUID result.
Passive native recognition below is permitted; dynamic factory inspection through custom metadata,
binding or attribute-access hooks is not. Do not invoke the factory or probe arity during construction.
This clarification also governs the construction prohibition in the [row contract](06-worker-repository-row-contract.md#preserved-construction-and-public-boundary).
The factory hook-safety boundary does not prohibit already-approved acquisition-timeout numeric conversion.

### FR2 — One-level exact-native descriptor recognition

Recognize stored targets only for:

- Exact native `staticmethod`, through binding or direct descriptor-object invocation.
- Exact native `classmethod`, through attribute binding.

An exact native classmethod supplied directly fails callability validation.

Classify using actual exact types, not `x.__class__` or subclass membership as proof of native behavior.

Acquire the stored target through the trusted native `__func__` descriptor. Recognize it only when directly:

- An exact native function; or
- An exact native bound method whose native `__func__` is an exact native function.

Method-to-function extraction is the only additional level at this descriptor boundary. Nested descriptors, partials and other stored targets are opaque.

An outer exact partial may reach an eligible descriptor. A partial behind the descriptor boundary cannot reopen traversal.

Native async-descriptor support and the narrower stopping rule are approved.

### FR3 — Descriptor-subclass stored-target opacity

All staticmethod/classmethod subclasses are opaque for stored-target extraction, including subclasses inheriting native binding.

Do not invoke custom binding to discover its result.

A supplied descriptor-subclass object can independently expose a safely acquired native asynchronous effective `__call__`. Such evidence concerns its direct invocation—not its unused stored target.

This distinction covers both directions:

- Stored async function with synchronous custom binding.
- Stored synchronous function with asynchronous custom binding.

### FR4 — Durable hook-safety wording and native mechanism

Approved durable wording:

> Constructor-time factory recognition prevents custom metadata, binding, instance/metaclass attribute-access, dictionary-method, dictionary-key comparison and indirect user-object hashing hooks from executing. Preventing execution—not catching hook exceptions—is the requirement.

The mechanism must protect both acquisition and subsequent processing.

Do not pass supplied factories, user-reachable functions, descriptors or namespaces to:

- `inspect.getattr_static`;
- `inspect.iscoroutinefunction`;
- `inspect.isasyncgenfunction`.

FR11’s narrowly controlled inspect calls on fresh internal functions are the explicit exception. They do not inspect a supplied factory.

Examine native coroutine and async-generator code flags before optional marker metadata. Ambiguous marker metadata cannot conceal native async code.

The approved native mechanism excludes the installed inspect cache/weak-reference path as well as ordinary metadata lookup.

This safety boundary assumes unmodified trusted native primitives and standard-library bindings. It is not an interpreter sandbox and does not cover global monkeypatching, audit hooks, concurrent mutation or unrestricted resource exhaustion.

### FR5 — Exact-partial traversal

Unwrap only exact native `functools.partial`, using its trusted native `func` descriptor.

Nested exact partials remain recognized without a depth limit. Subsequent descriptor boundaries retain FR2’s stopping rule.

Partial subclasses are opaque for stored-target extraction, including subclasses inheriting native partial calling behavior.

For the original supplied factory, independently recognized native asynchronous overridden `__call__` still rejects construction. A synchronous override remains accepted absent other permitted evidence.

Outside a descriptor boundary, exact native bound-method extraction may lead to existing recognized forms. Inside one, only a directly function-backed method qualifies.

No `partialmethod`, generic `__wrapped__`, proxy or arbitrary function-like support is introduced.

### FR6 — Binding, direct invocation and independent roots

Distinguish:

- A raw special-method descriptor used through `__get__`.
- A descriptor object supplied directly and invoked through its effective `__call__`.

An opaque binding descriptor’s own direct-call method does not prove what its binding supplies.

There are only two independent roots:

1. The original supplied factory.
2. Its safely acquired raw effective type-level `__call__`, when unambiguously observed.

Do not create roots from opaque inner objects or follow `__wrapped__`.

Consequently:

- A subclass storing an async target but binding synchronously is accepted absent other permitted evidence.
- A subclass storing a synchronous target but binding asynchronously is accepted; invocation validation rejects its invalid result.

Normal binding can execute during actual factory invocation. Constructor safety is not a guarantee against invocation-time user code.

### FR7 — Stable-graph repetition disposition

Traverse iteratively.

For each recognition root, strongly retain visited exact partial objects. Reaching the same exact partial again terminates that path as opaque.

Use identity comparisons only. An owned integer-ID index is permissible while strong references prevent identity reuse. Do not hash or compare supplied objects.

No depth limit, new public error or factory invocation is introduced.

Every cycle in the approved stable graph passes through an exact partial:

- Functions terminate.
- Descriptor paths terminate after the direct target check.
- Opaque objects terminate.
- Immutable method links cannot alone create a cycle.
- Exact-partial targets provide the mutable links in the demonstrated cycles.

This is not a termination guarantee under concurrent mutation/resource exhaustion, nor a deadline for later invocation of a cyclic factory.


### FR8 — Retained invocation and cleanup obligations

[§3](#3-lease-uuid-factory-contract), [§6](#6-independent-review-qualifications-and-source-identity)
and [row-contract C4](06-worker-repository-row-contract.md#c4--claim-observations-and-branches)
retain their approved factory placement/counts, exact nonnil UUID/object preservation, invalid-result
handling, exception identity, rollback/release and cleanup precedence. Close only an actual unstarted
native coroutine; never await invalid results or operate on arbitrary awaitables. No internal retry
or global UUID-history cache is introduced. Opaque construction acceptance weakens none of these obligations.

### FR9 — Non-normative Python qualification

Repository configuration declares Python `>=3.12`; Ruff and Mypy target 3.12. Recorded feasibility evidence is CPython 3.12.13, normally and with `-O`; its provenance is in the [decision record](07-worker-factory-recognition-decision.md#evidence-provenance-and-limits).

The narrowed descriptor rule removes the need for a nested-descriptor recognition branch. It does not remove all interpreter differences. In particular, classmethod descriptor chaining was removed in Python 3.13. No execution coverage of another version is claimed. [Python documentation](https://docs.python.org/3.13/library/functions.html#classmethod)

Compatibility maintenance remains necessary across the declared range. A bootstrap rejection diagnoses incompatibility with this approved recognition profile; it does not silently redefine the project’s supported versions.

### FR10 — Conservative exact-string evidence

This conservative exact-string evidence policy is explicitly approved.

Observe namespace/metadata entries through native iteration without hash-based name lookup.

For each lookup:

- Only exact built-in str keys establish target-name evidence.
- Any non-exact-string key makes the entire lookup opaque.
- This includes matching or unrelated string subclasses and non-string keys.
- Opacity dominates a coexisting exact target entry.
- An opaque earlier MRO namespace stops inheritance lookup.
- Native async code flags remain independent evidence.
- Opaque function metadata supplies no marker evidence.

A textually unrelated key can intercept effective lookup through custom hashing/equality. Text-only ambiguity handling is therefore insufficient.

Derived marker names use the same exact-string observation rule. Marker values are compared only by identity with FR11’s cached token.

| Observation                                  | Meaning                                      |
| -------------------------------------------- | -------------------------------------------- |
| Exact-string marker entry with derived token | Known asynchronous                           |
| Absent marker                                | No marker evidence                           |
| Exact-string marker entry with another value | No marker evidence                           |
| Any non-exact-string key                     | Opaque lookup                                |
| Native coroutine/async-generator flags       | Known asynchronous independently of metadata |

This does not emulate arbitrary hook-dependent Python lookup.

### FR11 — Approved import-time marker-bootstrap contract

Import-time initialization is explicitly approved. Construction-time bootstrap is not adopted; it is recorded only as a considered alternative in the [decision record](07-worker-factory-recognition-decision.md#considered-not-adopted-alternatives).

#### Trigger and placement

During ordinary execution of the canonical adapter module:

1. Complete its ordinary imports.
2. Establish the module-private trusted native access primitives and bootstrap helper.
3. Run bootstrap once.
4. Publish the successful private profile.
5. Continue to definition of the concrete repository class and remaining module initialization.

Bootstrap therefore precedes availability of a successfully imported concrete constructor. It does not move a bootstrap step into constructor validation.

Ordinary imports themselves use Python’s loader; “no I/O” below concerns bootstrap’s own work, not a claim that Python loads source without filesystem activity.

#### Accepted profile and controls

Use only two fresh, internally created exact native functions: a marked probe and a separate unmarked probe.

Their bodies maintain private, bootstrap-local execution counters. Neither body is called.

The accepted bootstrap requires all of the following:

1. Both functions initially have empty native metadata.
2. Both initially lack `CO_COROUTINE` and `CO_ASYNC_GENERATOR`.
3. Call public `inspect.markcoroutinefunction` once on the marked probe.
4. Ignore its returned object; derive evidence from the original fresh marked function.
5. Through native function-dictionary acquisition and unbound `dict.items`, observe exactly one metadata entry on that marked function.
6. Its key is an exact built-in str. Its spelling is derived, not hard-coded.
7. The separate unmarked function still has empty metadata.
8. Both functions still lack the two native async flags.
9. Public `inspect.iscoroutinefunction(marked_probe) is True`.
10. Public `inspect.iscoroutinefunction(unmarked_probe) is False`.
11. Both body-execution counters remain zero after the controls.

Validate the controlled metadata shape before the inspect predicates. Do not give those predicates malformed/custom-key metadata.

The entry’s value is the marker token. No additional token type/value restriction is needed: later recognition uses identity only.

These inspect calls touch only fresh internal functions and their validated, controlled metadata. They never touch a supplied factory or user-reachable function.

Any failed structural or Boolean/body-execution control is a profile failure.

#### Profile-failure disposition

Raise an explicit exact built-in:

```
RuntimeError("Lease UUID factory coroutine-marker bootstrap failed.")
```

It has exactly that one message argument.

Use an explicit conditional raise, not an `assert` statement. Mandatory controls and failure behavior therefore remain active under `python -O`.

This is an internal initialization/compatibility failure, not either Worker persistence error. Add no public exception class or export.

Do not silently disable marker recognition or fall back to inspect predicates on supplied factories.

#### Public-API and unexpected exceptions

Exceptions raised by `inspect.markcoroutinefunction` propagate unchanged by identity. Do not catch and translate them.

Unexpected exceptions from the permitted internal primitives or public predicate calls also propagate unchanged. Cancellation, `MemoryError` and fatal exceptions are not translated.

A failed control returning an unsupported result is different from an API call raising: the former receives the fixed profile `RuntimeError`; the latter retains the original exception.

#### Cause and context

Do not apply Worker persistence sanitization rules to this initialization boundary.

For the fixed profile error:

- Add no explicit cause.
- Do not clear or suppress Python’s normal exception context.
- In an ordinary isolated import with no active surrounding handler, cause/context are naturally absent.
- Import performed inside a caller’s active exception handler can acquire that caller exception as context.

Propagated API exceptions are not reconstructed and receive no explicit adapter cause/context rewriting. Ordinary Python chaining remains applicable.

No supplied-factory data is handled by bootstrap. This is not a promise to sanitize arbitrary surrounding caller exceptions.

#### Cache ownership and publication

Keep the successful profile as one module-private tuple containing the exact marker name and token.

Publish that tuple only after every control succeeds. Retain its components strongly. Do not publish a partially validated profile.

The profile is immutable **by design**, not protected from module monkeypatching. No failure object, probe function or supplied factory is cached.

The private helper, profile, name, token and native accessors remain outside `__all__`.

#### Import and initialization lifecycle

| Event                                                | Required behavior                                                                 |
| ---------------------------------------------------- | --------------------------------------------------------------------------------- |
| First successful ordinary import                     | One bootstrap execution and one marker-API call                                   |
| Later ordinary import of the cached module           | No bootstrap                                                                      |
| Constructor call after successful import             | Uses the published profile; no bootstrap                                          |
| Concurrent ordinary imports of the canonical module  | Use Python’s normal module-import synchronization; no adapter initialization lock |
| First import fails during bootstrap                  | No successful profile publication; ordinary import fails                          |
| Fresh ordinary import after that failed first import | A new module execution attempts bootstrap again                                   |
| Successful explicit sequential `importlib.reload`    | Bootstrap runs once for that execution; publishes a newly validated tuple         |
| Failed explicit reload                               | Bootstrap failure before publication: no new profile; previous tuple identity unchanged. Later initialization failure after publication: the new profile and partially updated globals can remain. |
| Another explicit reload after failure                | Attempts bootstrap again; no adapter retry loop                                   |

Python removes the failing target module after an ordinary first-import failure. Reload instead executes in the existing module dictionary; it is not transactional.

Accordingly, bootstrap failure before publication during reload leaves the previously published profile tuple as the same object. Later module initialization failure after publication can leave the newly published profile and other partially updated globals. An ordinary cached import can return that existing module without another bootstrap. **Neither case implies that the reloaded module is safe to use or that module state was rolled back.** It adds no revocation/repair mechanism.

Concurrent reload, use during reload, circular-import access to partially initialized modules, nonstandard loader execution and global mutation are outside the initialization guarantee. Normal successful import is the supported construction precondition.

The lifecycle distinctions are derived from inspected installed importlib source, not an executed production bootstrap.

#### Effects and inactivity

Bootstrap performs only internal allocation, native inspection and the stated public API controls. It performs:

- No I/O or network operations of its own.
- No pool access.
- No supplied-factory access or invocation.
- No execution of either probe body.
- No SQL, worker composition, loop or analysis activation.

An incompatible profile can prevent adapter import. Every test module that imports it during collection can consequently fail collection; lazily importing tests may fail later instead.

Ordinary static type analysis does not execute the adapter. Importing tooling/plugins remain subject to the import failure.

This module-private pure capability initialization is explicitly permitted. The prohibition on runtime wiring and external effects remains unchanged; policy approval does not authorize implementation or activation.

### Approved native mechanism — FR4, FR6, FR10 and FR11

The recognition facility remains private and bounded.

Permitted primitives:

- `type`, `callable` and identity comparisons.
- Trusted native type descriptors for MRO/class namespaces.
- Trusted native function descriptors for code/metadata.
- Trusted native code flags.
- Trusted native method, staticmethod/classmethod and partial target descriptors.
- Unbound `dict.items`, including dictionary subclasses.
- Unbound native mappingproxy entry iteration.
- Native tuple/view iteration.
- Exact-string name comparison.
- Native integer flag operations.
- Owned identity bookkeeping with retained references.
- FR11’s restricted public marker/predicate controls on fresh internal functions.

Do not use ordinary `getattr`, `hasattr`, user-bound `.items()`, membership, subscription or hashing on user-reachable namespaces/metadata.

Accessing descriptors through trusted built-in type dictionaries is distinct from querying a factory’s dictionary.

Entry observation must distinguish absent, present and opaque results using owned tags; arbitrary stored values cannot masquerade as those tags.

For raw call acquisition, visit native MRO namespaces in order. A present entry supplies the raw root. An opaque namespace terminates lookup rather than continuing into inheritance.

For native functions:

1. Check native coroutine/async-generator flags.
2. Otherwise observe metadata under FR10 using FR11’s derived exact-string name.
3. Recognize only token identity.

A changed or failed bootstrap profile never enables an unsafe fallback.

### Current defect inventory and required repairs — FR4

This inventory records the reviewed current implementation, not completed repairs. Demonstrated
metaclass hooks, key equality and weak-reference/cache-key hashing remain pre-commit blockers.
Repair and verification require separate production/test authorization.

| Current site/path                                                | Current problem                                                      | Required exclusion or replacement                                                       |
| ---------------------------------------------------------------- | -------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| Effective-call `inspect.getattr_static`                          | Metaclass namespace attribute hooks and namespace-key equality       | Native MRO/namespaces plus FR10 observation                                             |
| `getattr_static` → `_shadowed_dict` → cached weak-reference keys | Hashing a weak reference can invoke custom meta-metaclass `__hash__` | No inspect cache/weak-reference path; native acquisition and owned identity bookkeeping |
| Native-function asynchronous predicates                          | Coroutine-marker lookup can execute key equality                     | Native flags, entry iteration and marker identity                                       |
| Marker `getattr` inside inspect                                  | Effective dictionary lookup can invoke custom key behavior           | FR10 entry observation                                                                  |
| Bound-method target extraction                                   | Getter is native-safe; target is not necessarily a function          | Native extraction with FR2’s context-sensitive stopping                                 |
| Descriptor classification/unwrapping                             | Subclass membership does not establish actual binding                | Exact-native one-level rule; subclass opacity                                           |
| Partial native-call proof                                        | Unsafe static inspection and incomplete binding proof                | Exact-partial policy; no subclass stored-target traversal                               |
| Partial target extraction                                        | Native getter is safe; targets can repeat                            | Native extraction with FR7                                                              |
| Raw namespace name processing                                    | Safe acquisition followed by ordinary lookup remains unsafe          | Native iteration, exact-string evidence and opacity                                     |
| Instance shadows/metadata                                        | Dynamic accesses inspect irrelevant fields or execute hooks          | Actual type and only the two permitted roots                                            |
| Runtime coroutine-return checks                                  | Outside constructor recognition                                      | Preserve FR8 without expansion                                                          |

### Required acceptance outcomes — FR1–FR7 and FR10

These are approved outcomes for the repaired recognizer, not claims of executed replacement evidence.
All construction cases require zero supplied-factory invocations, zero pool interactions and zero
custom recognition hooks under the stated assumptions. The separate historical invocation measurements
and their limits remain evidence in the [decision record](07-worker-factory-recognition-decision.md#evidence-provenance-and-limits), not constructor guarantees.

- `A`: construction accepted; FR8 invocation validation remains mandatory.
- `C`: `TypeError("Lease UUID factory must be callable.")`.
- `S`: `TypeError("Lease UUID factory must be synchronous.")`.

If an accepted invocation returns an unstarted native coroutine, raise the fixed exact-UUID type
failure without awaiting, close that coroutine and roll back/release. Other invalid results,
including async generators, receive the same fixed type failure without user-object cleanup hooks.
A valid exact nonnil UUID is preserved. Invocation-time binding/key exceptions retain identity;
a permanently raising runtime key is not promised successful invocation.

#### Controls, metadata and keys

| Factory form / actual call path | Required construction outcome |
| --- | --- |
| Raising metadata, `__getattr__`, instance `__class__`, `__wrapped__` controls | A |
| Recording metaclass, sync call | A |
| Recording metaclass, async call | S |
| Raising metaclass, sync call | A |
| Recording/raising meta-metaclass hash | A |
| Ordinary sync function | A |
| Direct async function | S |
| Direct async generator | S |
| `uuid.uuid4` | A |
| Ordinary class factory | A |
| Canonically marked synchronous function | S |
| Recording marker-key equality | A |
| Raising marker-key equality | A |
| Recording namespace-key equality, sync target | A |
| Raising namespace-key equality | A |
| Wrong-hash marker-name key | A |
| Same-hash marker key with false equality | A |
| Unrelated key intercepts exact marker | A |
| Coexisting exact/subclass marker keys | A |
| Harmless integer metadata plus canonical marker | A |
| Non-string key impersonates marker | A |
| Wrong marker value with raising equality | A |
| Native coroutine with raising marker key | S |
| Native async generator with raising marker key | S |
| Ambiguous earlier namespace, inherited async call | A |
| Function dictionary subclass overriding methods | S |

#### Descriptor and namespace paths

| Factory form / actual call path | Required construction outcome |
| --- | --- |
| Direct exact staticmethod of async function | S |
| Exact classmethod supplied directly | C |
| Namespace `__call__ = classmethod(async_fn)` | S |
| Namespace `__call__ = staticmethod(async_fn)` | S |
| Exact staticmethod of function-backed bound method | S |
| Subclass stores async, binds sync | A |
| Subclass stores sync, binds async | A |
| Supplied subclass stores async, sync direct override | A |
| Supplied subclass stores sync, async direct override | S |
| Inherited static subclass with async target | A |
| Nested native staticmethod | A |
| Staticmethod → exact partial async target | A |
| Exact partial → native staticmethod async target | S |
| Staticmethod → method backed by partial | A |
| Namespace `__call__ = None` | A |

Inherited classmethod-subclass binding is similarly opaque by policy. A directly supplied classmethod
subclass without a custom callable method remains noncallable. Normal binding may execute at invocation,
not during construction.

#### Partial paths

| Factory form / actual call path | Required construction outcome |
| --- | --- |
| Exact partial, synchronous target | A |
| Exact partial, asynchronous target | S |
| Nested exact partials, async target | S |
| Native bound method backed by async function | S |
| Inherited partial subclass, sync target | A |
| Inherited partial subclass, async target | A |
| Partial subclass, synchronous call override | A |
| Partial subclass, asynchronous call override | S |
| Partial metadata override, sync target | A |
| Partial metadata override, async target | A |
| Partial subclass stores async, binds sync | A |
| Partial subclass stores sync, binds async | A |
| Exact partial wrapping inherited async partial subclass | A |
| Namespace `__call__ = exact partial(async_fn)` | S |
| Namespace `__call__ = partial subclass(async_fn)` | A |

### Future regression obligations — FR1–FR11

These are requirements for later, separately authorized implementation and tests. This documentation task authorizes no test edits or execution.

#### Bootstrap isolation and preflight

Use child-process isolation for bootstrap variations, optimization and reload cases. Do not mutate shared parent-process inspect state.

Before the adapter import, the child must:

1. Use the authorized project interpreter and existing dependencies.
2. Establish the project source import path.
3. Verify/import required dependencies, including Psycopg/Psycopg pool and the domain/service dependencies; Pydantic is required transitively.
4. Confirm the target adapter has not already been imported.
5. Emit a preflight-success phase marker.
6. Install the controlled public-API variation.
7. Import the adapter through its canonical name.

A missing dependency, wrong import path, transitive import error or syntax defect fails the test as a genuine harness/implementation failure. It is not accepted as the target bootstrap failure.

Do not use “import failed” as the oracle. Verify phase, variation-call count, exact error and lifecycle outcome.

#### Required bootstrap cases

| Future case                            | Controlled variation installed before import                                    | Required assertion                                                                   |
| -------------------------------------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Valid profile                          | Recording delegate to original public marker API                                | One call; successful import; no extra call on ordinary cached import or construction |
| Missing profile                        | Return the fresh input without marking                                          | Exact built-in RuntimeError and exact single message                                 |
| Ambiguous profile                      | Mark, then add another metadata entry                                           | Same fixed profile error                                                             |
| Non-exact-key profile                  | Replace derived entry with a non-exact-string-key form                          | Fixed error; no unsafe supplied-object fallback                                      |
| Wrong single entry                     | Supply one exact-string entry not satisfying predicate controls                 | Fixed error                                                                          |
| Failed marked/unmarked control         | Controlled public predicate variation on internal probes                        | Fixed error                                                                          |
| Native async flag alteration           | Controlled marker variation changes the fresh probe code to a native async form | Fixed error                                                                          |
| Probe-body execution                   | Controlled API variation invokes the fresh probe and returns                    | Counter control fails with fixed error                                               |
| API exception                          | Raise a preconstructed marker exception                                         | Captured exception `is` the original; no adapter translation or explicit chaining    |
| Fatal/API failure                      | Raise controlled fatal exception                                                | Identity propagation; no broad translation                                           |
| Optimization                           | Run profile-failure cases with `-O`                                             | Same fixed error; not dependent on asserts                                           |
| First-import failure then fresh import | First variation fails; then install valid delegate and explicitly import again  | Target absent after first-import failure; later attempt bootstraps and succeeds      |
| Successful reload                      | Import validly, then explicitly reload                                          | One further bootstrap/API call                                                       |
| Failed reload                          | Import validly, then install a failing bootstrap variation and reload before profile publication | Exact bootstrap failure; previously published tuple identity unchanged; do not claim module rollback |
| Reload recovery                        | After failed reload, install valid delegate and explicitly reload               | Reattempt succeeds                                                                   |

For ordinary isolated profile failures, assert `__cause__ is None` and `__context__ is None`. Separately, an import inside an active caller handler should retain ordinary Python context rather than claim persistence sanitization.

API-exception tests must assert identity and lack of adapter rewriting. An explicitly preconfigured cause can be used to distinguish unchanged propagation from reconstruction.

Each child has an external timeout and bounded termination/reaping. Timeout is a failing result, not a skip/xfail. Do not install dependencies or silently continue when preflight fails.

Controlled API variation is test instrumentation, not a replacement recognizer or policy-producing fake.

#### Recognition regressions retained

Keep focused coverage for:

- Recording/raising metaclass and meta-metaclass hooks.
- Recording/raising key equality; wrong-hash and false-equality cases.
- Unrelated intercepting keys, coexisting keys and earlier opaque namespaces.
- Harmless non-string metadata plus a canonical marker.
- Marker absent/wrong/canonical cases and independent native flags.
- Function-dictionary subclasses and native getter controls.
- Original/inverse binding cases and direct overrides.
- Native descriptor controls and one-level stopping.
- Exact/nested partials and partial-subclass opacity.
- Explicit namespace staticmethod/classmethod/partial/partial-subclass/None cases.
- Prior instance shadows, metadata protections and validation order.
- UUID identity, factory counts, coroutine cleanup and exception precedence.
- A behavior-based cyclic-constructor child probe with an external bound; no adapter line-number oracle.

Preserve all unrelated existing assertions and parametrizations.

Post-publication initialization failure is a documented qualification, not a newly added production
regression. Earlier generic-loader measurements are not production-bootstrap verification.
No replacement recognizer or bootstrap is established by this documentation.

No public exports, error aliases, constructor parameters, operation signatures, SQL, lifecycle,
snapshots, Fingerprint V1/HMAC framing, callbacks or runtime wiring change. Outstanding implementation
and verification obligations remain blockers before GREEN-H commit approval. Live locking, uniqueness
arbitration, mutation clocks, recovery/reclaim fencing, downgrade protection and runtime activation
retain their separate gates.
