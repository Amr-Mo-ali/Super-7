# PLANNING-H — Approved Worker Repository Row Contract

Status: **C1–C10 explicitly approved; documentation-fidelity review pending.** The project owner separately approved C9a and C9b as contract extensions. This records the designated corrected proposal without incorporating the incomplete first draft. Authorization is documentation only; RED-H, GREEN-H, database execution and runtime activation remain unauthorized.

Read with the protected [Planning-F contract](04-worker-claim-lease-fencing-contract.md) and [Planning-H construction contract](05-worker-repository-construction-contract.md).

The documentation starting gate passed:

- Branch: `feat/sprint-2-worker-claim-lease-fencing`
- HEAD: `7f4c215febc132676b5080e14d27d75681c94c78`
- `origin/main`: `634f0d4872ae6d3c13467eda25bd23d2c9ab1a47`
- Divergence: **0 behind / 4 ahead**
- Clean worktree; empty index; no upstream or remote Worker branch.

Historical provenance recorded in the corrected proposal: the old two-draft attachment measured **590 lines / 60,625 bytes**, SHA-256:

```
8a3e292d5c102980117e6970bde50c29732e314dc30d01af99e2d4c6dd463882
```

The corrected proposal used only that attachment’s complete candidate, lines 150–590. The documentation starting gate verified Planning-F/H, README, and all three migrations against their reviewed identities and committed bytes. The authoritative corrected input’s own identity is recorded below; the old attachment identity is not this document’s source identity.

## Approval and evidence boundary

The implemented Worker domain/service values, current pool surfaces, and committed migration source are verified existing facts. Their existence does not establish an implemented Worker repository or executed database migration.

Planning-F/H already approve the public operations, construction boundary, timeouts, factory contract, snapshots, fencing, lifecycle, error taxonomy, and inactivity requirements.

**C1–C10 below are explicitly approved concrete row/observation decisions.** Approval comes from the project owner’s explicit authorization, not from repetition of previously approved requirements or reuse of existing types. Implementation and live evidence remain separate gates.

| ID  | Approved decision                                                              |
| --- | ------------------------------------------------------------------------------ |
| C1  | Exact `J`, `A`, `L`, and `T` layouts and domain mapping                        |
| C2  | Explicit result cardinality, read predicates, and absence meanings             |
| C3  | Separate authoritative reads, job-before-attempt locks, and resource lifecycle |
| C4  | Same-slot discovery, revalidation, and fresh-claim observations                |
| C5  | Absolute-instant arithmetic and single-sample mutation-clock requirements      |
| C6  | Independently checkable mutation `RETURNING` evidence                          |
| C7  | Bounded recovery selection, revalidation, and accounting                       |
| C8  | Committed-pair consistency, validation precedence, and replay                  |
| C9  | Two explicitly approved extensions to contention classification                |
| C10 | RED-H coverage and separation of scripted, source, and live evidence           |

## Preserved construction and public boundary

Module:

```
adapters.psycopg_analysis_job_worker_repository
```

Public exports, in the approved order:

```
__all__ = [
    "PsycopgAnalysisJobWorkerRepository",
    "AnalysisJobWorkerPersistenceUnavailable",
    "AnalysisJobWorkerPersistenceInvariantError",
]
```

Both error exports are aliases to the existing service classes **by identity**, not new definitions or subclasses.

The required constructor remains:

```
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

The private structural ports remain unchanged:

| Surface    | Signature                                                                           |
| ---------- | ----------------------------------------------------------------------------------- |
| Cursor     | `async fetchone() -> tuple[object, ...] \| None`                                    |
| Cursor     | `async fetchall() -> list[tuple[object, ...]]`                                      |
| Cursor     | `async close() -> None`                                                             |
| Connection | `transaction() -> AbstractAsyncContextManager[object]`                              |
| Connection | `async execute(query: str, params: tuple[object, ...] \| None = None) -> _Cursor`   |
| Pool       | `connection(*, timeout_seconds: float) -> AbstractAsyncContextManager[_Connection]` |

Construction does not inspect or invoke the pool or factory. Real `PsycopgDatabasePool` structural compatibility remains a required future strict-Mypy composition witness without casts or suppression.

Read this construction prohibition with [Planning-H §7](05-worker-repository-construction-contract.md#7-approved-constructor-time-factory-recognition), which explicitly permits the approved passive native factory-recognition and pure module-initialization rules (FR1–FR11) while prohibiting factory invocation and custom recognition hooks; this cross-reference changes no row layout, operation, lifecycle, snapshot or fencing rule.

The five operation signatures remain:

```
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

Timeout validation remains ordered:

1. Acquisition timeout.
2. Statement timeout.
3. Lock timeout.
4. `operation_timeout_ms > lock_timeout_ms`.
5. Factory configuration.

Acquisition accepts `isinstance(value, (int, float))`, excluding bool, and normalizes once to a positive finite float. Each millisecond timeout requires exact built-in `int` in `1..2_147_483_647`. Existing fixed validation failures remain unchanged.

Every operation executes and closes, in order:

```
SQL: SELECT set_config(%s, %s, %s)

("statement_timeout", str(operation_timeout_ms), True)
("lock_timeout", str(lock_timeout_ms), True)
```

These are transaction-local settings preceding durable-table statements. Statement timeout is **per statement**, not a total-operation deadline. No advisory lock, operation-specific override, or independent commit is introduced.

## C1 — Exact row layouts

Positions are zero-based projection positions, not physical table-column order.

Common decoding rules:

- UUID: `type(value) is UUID`, nonnil; no version restriction.
- Integer: exact built-in `int`; bool and subclasses rejected.
- Text: exact built-in `str`; no coercion or trimming.
- Timestamp: `isinstance(value, datetime)` with a valid non-null UTC offset.
- Nullable fields accept `None` only where specified.
- Rows are exact built-in tuples of the specified length.
- Accepted timestamp objects are passed into public values unchanged, preserving their identity, offset, and fold.
- Repeated database reads need not return the same Python object. Timestamp-column preservation across reads is checked by absolute instant, not object identity.
- No UTC conversion or additional timestamp-range restriction is introduced.

### J — Worker job row: 10 columns

Used by authoritative job reads, fresh selection, recovery selection, and job mutation `RETURNING`.

| Position | Column             | PostgreSQL / Python            | Nullable |
| -------- | ------------------ | ------------------------------ | -------- |
| 0        | `job_id`           | UUID / exact `UUID`            | No       |
| 1        | `video_id`         | TEXT / exact `str`             | No       |
| 2        | `player_id`        | TEXT / exact `str`             | No       |
| 3        | `video_reference`  | TEXT / exact `str`             | No       |
| 4        | `callback_url`     | TEXT / exact `str`             | No       |
| 5        | `analysis_version` | TEXT / exact `str`             | No       |
| 6        | `state`            | TEXT / exact `str`             | No       |
| 7        | `attempt_limit`    | INTEGER / exact `int`          | No       |
| 8        | `attempt_count`    | INTEGER / exact `int`          | No       |
| 9        | `accepted_at`      | TIMESTAMPTZ / aware `datetime` | No       |

Validation:

- `video_id` and `player_id`: full match of `[A-Za-z0-9_-]+`.
- `video_reference`: exactly Planning-F’s frozen filename predicate; no added platform-specific filename restriction.
- `callback_url`: exact built-in nonempty canonical string, validated through the approved `HttpUrl` surface, with no fragment.
- `analysis_version`: full match of `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`.
- `state`: exact `AnalysisJobState` vocabulary.
- `attempt_limit > 0`.
- `0 <= attempt_count <= attempt_limit`.

`ClaimableAnalysisJob` receives positions **0–5, 7, 9**, in its approved field order.

`attempt_count` remains private persistence state. Neither it nor job state is added to the execution payload. Protected lookup/fingerprint material is absent from `J`.

There is no new `accepted_at <= started_at` invariant.

### A — Attempt row: 10 columns

Used by authoritative attempt reads and attempt INSERT/UPDATE `RETURNING`.

| Position | Column              | PostgreSQL / Python            | Nullable |
| -------- | ------------------- | ------------------------------ | -------- |
| 0        | `job_id`            | UUID / exact `UUID`            | No       |
| 1        | `attempt_number`    | INTEGER / exact positive `int` | No       |
| 2        | `worker_slot_id`    | UUID / exact `UUID`            | No       |
| 3        | `lease_id`          | UUID / exact `UUID`            | No       |
| 4        | `status`            | TEXT / exact `str`             | No       |
| 5        | `started_at`        | TIMESTAMPTZ / aware `datetime` | No       |
| 6        | `heartbeat_at`      | TIMESTAMPTZ / aware `datetime` | No       |
| 7        | `lease_expires_at`  | TIMESTAMPTZ / aware `datetime` | Yes      |
| 8        | `finished_at`       | TIMESTAMPTZ / aware `datetime` | Yes      |
| 9        | `safe_failure_code` | TEXT / exact `str`             | Yes      |

Status vocabulary remains the implemented `AttemptStatus(StrEnum)`:

```
ACTIVE
INTERRUPTED
RETRYABLE_FAILURE
TERMINAL_FAILURE
EXHAUSTED
```

An eligible ACTIVE `A` and associated `J` map to `WorkerClaim`. Resolved attempts remain private decoded data; no new public attempt model or decoder API is introduced.

### L — Slot locator: 2 columns

| Position | Column           | Python               |
| -------- | ---------------- | -------------------- |
| 0        | `job_id`         | Exact nonnil `UUID`  |
| 1        | `attempt_number` | Exact positive `int` |

Predicate:

```
status = ACTIVE
AND worker_slot_id = supplied slot UUID
```

`L` conveys discovery only. It contains no lease or timestamp and does not establish mutation authority.

### T — Database-clock observation: 1 column

| Position | Expression          | Python           |
| -------- | ------------------- | ---------------- |
| 0        | `clock_timestamp()` | Aware `datetime` |

Exactly one row is required.

`T` is an observed database-clock sample. It is not a receipt for a later mutation’s clock sample. PostgreSQL distinguishes actual `clock_timestamp()` from transaction-start `CURRENT_TIMESTAMP`. [PostgreSQL datetime functions](https://www.postgresql.org/docs/current/functions-datetime.html)

## C2 — Cardinality, predicates, and absence meanings

Use `fetchall()` for `J/A/L/T` observations and mutation results. The result must be a list; each element must be an exact tuple. Do not truncate duplicate singleton results or infer success from cursor creation.

Notation: `J[1]` means exactly one valid `J`; `A[0]` means an empty result.

| Observation                                                  | Exact selection meaning                                                                                      | Zero-row meaning                                                                                         |
| ------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| Initial `L`                                                  | ACTIVE attempts for supplied slot, without a duplicate-hiding `LIMIT 1`                                      | No ACTIVE ownership observed; not a reserved slot                                                        |
| Authority operation’s job read                               | Supplied `job_id`, unfiltered by state/count, `FOR UPDATE`                                                   | `ClaimLost`; no job was observed                                                                         |
| Current-attempt read                                         | PK `(locked J.job_id, locked J.attempt_count)`, without status/slot/lease/expiry filters                     | Invariant failure whenever positive count requires that current row                                      |
| Locator job read after `L[1]`                                | `L.job_id`, unfiltered, `FOR UPDATE`                                                                         | Invariant failure, not obsolete discovery                                                                |
| Locator attempt read after `L[1]`                            | PK `(L.job_id, L.attempt_number)`, unfiltered, `FOR UPDATE`                                                  | Invariant failure, not obsolete discovery                                                                |
| Fresh candidate selection                                    | QUEUED, count below limit, accepted-time/job-ID order, `FOR UPDATE SKIP LOCKED`, limit one                   | `NoJobAvailable`                                                                                         |
| ACTIVE-by-job check before fresh claim                       | ACTIVE rows for selected job                                                                                 | Required empty result; any returned ACTIVE row prevents a fresh claim and is validated for contradiction |
| Explicitly fenced eligibility observation/update             | Complete supplied fence, RUNNING, current count, ACTIVE, unexpired at that statement’s captured clock sample | Eligibility not established; owner mutation returns `ClaimLost`                                          |
| Recovery candidate selection                                 | Current expired ACTIVE/RUNNING candidates, bounded job-row `SKIP LOCKED` selection                           | No candidates observed                                                                                   |
| Recovery attempt lock                                        | Current PK with `FOR UPDATE SKIP LOCKED`                                                                     | Skip this candidate; the result does not distinguish lock-skipping from physical absence                 |
| Required job `RETURNING` after successful attempt transition | Associated locked job and intended transition                                                                | Invariant failure; roll back the whole transaction                                                       |
| Required fresh INSERT/job mutation `RETURNING`               | Intended mutation                                                                                            | Invariant failure                                                                                        |

All singleton reads reject more than one row. Recovery selection rejects a result exceeding the supplied batch size.

**Superseded supplied generation is not an absence classification.** For an existing job with positive count, decode and validate its authoritative current attempt first. Then a supplied generation differing from the valid current generation produces `ClaimLost`. Missing or contradictory current state must not be hidden by that stale request.

For a valid count-zero job, there is no current attempt to read. A supplied positive generation has no authority and returns `ClaimLost`.

The normative owner-operation sequence uses an unfiltered diagnostic read followed by a fenced UPDATE, not a filtered lookup that conflates missing current state with lost eligibility.

Recovery’s `SKIP LOCKED` zero result is deliberately weaker evidence: it grants no authority and produces no mutation. It is neither `ClaimLost` nor C9a recognition, and it does not prove that a required current row physically exists. No global corruption audit is claimed.

## C3 — Locks, transactions, and cleanup

Each operation:

1. Validates public inputs before pool interaction.
2. Acquires one connection.
3. Enters one transaction.
4. Executes and closes the two approved setting cursors.
5. Performs its prescribed observations and mutations.
6. Closes obtained cursors.
7. Commits ordinary outcomes, or rolls back failures.
8. Completes connection release before exposing an outcome or failure.

Authoritative locking is **job before attempt**. Locks remain held through mutation and transaction completion.

Separate `J` and `A` reads make missing-side treatment explicit. A discovery query or candidate-selection join does not replace these authoritative checks. Recovery joins must not acquire attempt locks before the job locks.

When a failure is recognized, establish its correctly classified primary before cleanup:

- Expected persisted-value failures become the fixed invariant error.
- Approved operational failures become the fixed unavailable error.
- Unexpected programming/factory/fatal failures remain unchanged.
- Ordinary cleanup failure does not replace an active primary.
- Fatal cleanup failure preserves identity and takes the already-sanitized primary as cause.
- Cleanup failure without a primary follows Planning-H’s narrow classification; no outcome escapes.

Expected domain-construction `TypeError`/`ValueError` translation surrounds only that construction boundary. Public errors are raised after leaving the raw handler.

Exact public messages:

```
PostgreSQL AnalysisJob worker persistence invariant failed.
PostgreSQL AnalysisJob worker persistence is unavailable.
```

Translated errors have one fixed message argument, no diagnostic attributes, and no retained raw cause/context. No rejected row, SQL, parameters, credentials, callback, fingerprint, HMAC state, or driver diagnostics are logged.

No second transaction, independent commit, internal retry, advisory lock, successful finalization, or outbox behavior is added.

## C4 — Claim observations and branches

### Same-slot discovery

After `L[1]`:

1. Lock and decode required `J[1]`.
2. Lock and decode required locator `A[1]`.
3. Verify locator job/generation and supplied slot association.
4. If the locator is historical, also read and validate the required current attempt under the job lock.
5. Apply C8 consistency before selecting an outcome.

| Authoritative observation                                                  | Result                                          | Factory |
| -------------------------------------------------------------------------- | ----------------------------------------------- | ------- |
| Current RUNNING ACTIVE, expiry strictly after post-lock `T`                | Exact persisted `WorkerClaim`; no writes        | 0       |
| Current RUNNING ACTIVE, expiry at/before `T`                               | `WorkerSlotUnavailable`; no writes              | 0       |
| Structurally consistent ACTIVE alongside CANCELLED                         | `WorkerSlotUnavailable`; no authority or writes | 0       |
| Valid resolved replacement satisfying every C9a condition                  | Approved unavailable failure; rollback/release  | 0       |
| Missing, malformed, incorrectly associated, or contradictory required rows | Invariant failure; rollback/release             | 0       |

No persisted retry value is regenerated or normalized.

### Fresh claim

After `L[0]`:

1. Select at most one QUEUED job below its limit, ordered by `accepted_at ASC, job_id ASC`, using `FOR UPDATE SKIP LOCKED`.
2. Decode selected `J`.
3. For positive count, read its unfiltered current attempt and validate the committed pair.
4. Check ACTIVE-by-job ownership; require no ACTIVE attempt.
5. Only after all required checks, invoke the factory once.
6. Validate its exact nonnil UUID.
7. Update the job to RUNNING with count `old_count + 1`, validating `RETURNING J[1]`.
8. Insert the new ACTIVE attempt, validating `RETURNING A[1]`.
9. Validate the final pair, close resources, commit, and release.
10. Return the complete `WorkerClaim`.

No candidate returns `NoJobAvailable` with zero factory calls.

The complete fresh authority is:

```
(selected job_id, old_count + 1, supplied worker_slot_id, generated lease_id)
```

Every component must be preserved. Generation two cannot silently become generation one.

The public new `LeaseId` preserves the factory UUID object. A returned database UUID must equal it; an equivalent newly decoded UUID is not silently substituted for the factory object.

Factory validation and exception behavior remain Planning-H’s approved contract:

- Exactly one synchronous no-argument invocation at the stated boundary.
- Exact UUID, nonnil, no version restriction or extra slot/lease inequality.
- Wrong type and nil use existing fixed failures.
- Factory exceptions—including driver-class exceptions—propagate unchanged.
- Only an actual unstarted native coroutine is closed when rejected.
- Arbitrary awaitables receive no cleanup hooks.
- No internal retry after generation or failure.

## C5 — Absolute time and mutation-clock evidence

### Exact absolute-instant arithmetic

For a validated aware datetime `d`, define the comparison key:

```
I(d) = (d.replace(tzinfo=None) - datetime.min) - d.utcoffset()
```

This is exact `timedelta` arithmetic, consistent with the implemented domain comparison. The timezone-free value is a temporary arithmetic operand, not a normalized replacement for the supplied timestamp.

Use:

```
absolute ordering: I(a) <, <=, ==, >=, > I(b)
elapsed duration:  I(expiry) - I(base) == supplied LeaseDuration.value
```

Do not use direct same-zone aware-datetime subtraction, POSIX floats, `total_seconds()` rounding, or conversion to a UTC datetime that can overflow at representable extremes.

Preserve accepted timestamp objects, offsets, and folds. No additional date range, duration maximum, monotonic-clock guarantee, or clock-clamping policy is introduced.

### Observation versus mutation sample

`T` supports a post-lock eligibility observation. A later mutating statement must independently enforce its boundary.

Each attempt mutation captures **one actual PostgreSQL clock sample `t_m`**, after the authoritative locks, and evaluates it once for all fields and gates intended to share that instant.

| Mutation                  | Shared use of `t_m`                                         |
| ------------------------- | ----------------------------------------------------------- |
| Fresh attempt INSERT      | `started_at`, `heartbeat_at`, and base of new expiry        |
| Renewal                   | Unexpired-lease gate, new heartbeat, and base of new expiry |
| Worker failure resolution | Unexpired-lease gate and `finished_at`                      |
| Expiry recovery           | Expired-lease gate and `finished_at`                        |

A query must not independently call an advancing clock expression for these supposedly shared uses. SQL spelling is not frozen, but single evaluation and reuse are required source semantics.

Worker-owned mutation requires:

```
old lease_expires_at > t_m
```

Recovery requires:

```
old lease_expires_at <= t_m
```

Lease arithmetic adds the supplied duration as exact **elapsed time**, not session-timezone-dependent calendar days. PostgreSQL’s timestamp/interval arithmetic can make calendar-day additions differ across DST; that behavior must not define lease duration. [PostgreSQL datetime arithmetic](https://www.postgresql.org/docs/current/functions-datetime.html)

### What the decoder can and cannot establish

The decoder can independently check:

- Timestamp types and status shapes.
- Absolute ordering.
- Preserved timestamp instants.
- Fresh start/heartbeat equality.
- Exact returned expiry-minus-base duration.
- Returned mutation timestamp versus the prior expiry, where that prior expiry was observed.

It **cannot** establish that a returned heartbeat or finish actually originated from `t_m`, merely by comparing it with an earlier `T`.

Therefore:

- No `returned_mutation_time == earlier_T` assertion is required.
- Earlier `T` is not treated as the mutation’s expected timestamp.
- A shape-valid forged base time can pass tuple-only checks while violating SQL clock provenance.
- Source review must establish the captured database clock, evaluation placement, reuse, predicates, and elapsed arithmetic.
- Later authorized integration must establish actual database behavior, including expiry between the preliminary observation and mutation.

These are separate evidence obligations, not interchangeable proofs.

## C6 — Mutation `RETURNING` requirements

Job writes return `J`; attempt writes return `A`.

Expected singleton writes require exactly one valid row. The only permitted zero-match mutation results are the explicit owner/recovery eligibility gates described below.

All writes validate:

- Exact layouts and field types.
- Intended job/generation association.
- Complete intended fencing tuple.
- Immutable job text, version, limit, and acceptance instant.
- Intended state/status/count/code changes.
- Attempt timestamp shape.
- Final committed-pair consistency before commit.

### Operation-specific evidence

| Mutation                        | Independently checkable returned evidence                                                                                                                                       |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Fresh claim                     | Job RUNNING; count exactly old+1; ACTIVE attempt with the intended complete fence; start and heartbeat equal in absolute time; expiry-minus-heartbeat exactly supplied duration |
| Renewal                         | Same fence and start instant; ACTIVE shape; heartbeat not before old heartbeat; new heartbeat strictly before old expiry; expiry-minus-new-heartbeat exactly supplied duration  |
| Retryable/terminal finalization | Same fence, start, and heartbeat instants; intended resolved status/code; null expiry; finish at/after heartbeat and strictly before old expiry                                 |
| Expiry recovery                 | Same fence, start, and heartbeat instants; intended INTERRUPTED or expiry-EXHAUSTED status; null code/expiry; finish at/after heartbeat and at/after old expiry                 |
| Associated job transition       | Intended state; unchanged count except fresh claim; unchanged policy and immutable inputs                                                                                       |

The relation to old expiry is checkable because the gate and returned stamp are required to share `t_m`. It still does not independently prove database-clock provenance.

Renewal need not preserve or monotonically increase the old expiry: a newly supplied duration determines expiry from the new heartbeat. No extra inequality is added.

### Owner operations

For renewal and both finalizers:

1. Read and lock the supplied job.
2. If present with positive count, read its **current** attempt by unfiltered PK.
3. Validate shapes, associations, and C8 committed consistency.
4. Compare supplied authority with the complete current stored authority.
5. Evaluate operation-specific replay or ACTIVE eligibility.
6. For a new ACTIVE mutation, obtain post-lock `T`.
7. Execute the guarded attempt transition using its independently captured `t_m`.

Branches:

| Branch                                                     | Outcome                                       |
| ---------------------------------------------------------- | --------------------------------------------- |
| Job absent                                                 | `ClaimLost`                                   |
| Valid count-zero job                                       | `ClaimLost`                                   |
| Valid current pair, supplied generation/slot/lease differs | `ClaimLost`                                   |
| Valid pair but job state denies authority                  | `ClaimLost`, subject to replay rules          |
| ACTIVE lease already expired at `T`                        | `ClaimLost`                                   |
| Guarded owner UPDATE returns zero                          | `ClaimLost`; no other writes                  |
| Renewal UPDATE returns valid `A[1]`                        | `LeaseRenewed` after commit/release           |
| Malformed/contradictory required or returned data          | Invariant failure; whole transaction rollback |

Failure finalizers update the attempt **before** the associated job:

| Finalizer             | Attempt result                   | Job result | Outcome            |
| --------------------- | -------------------------------- | ---------- | ------------------ |
| Retryable below limit | RETRYABLE_FAILURE, supplied code | QUEUED     | `AttemptRequeued`  |
| Retryable at limit    | EXHAUSTED, supplied code         | FAILED     | `AttemptExhausted` |
| Terminal              | TERMINAL_FAILURE, supplied code  | FAILED     | `AttemptFailed`    |

A zero-match attempt gate performs no job mutation. After successful attempt mutation, missing, duplicate, or inconsistent job `RETURNING` is an invariant failure and rolls back both writes.

## C7 — Recovery observations and bounds

Recovery performs:

1. The common connection/transaction/settings sequence.
2. One selection-time `T[1]`.
3. One candidate query with exactly the supplied SQL `LIMIT`.
4. Candidate ordering `job_id ASC`.
5. Job-row `FOR UPDATE SKIP LOCKED`.
6. Per candidate, current-attempt PK lookup using `FOR UPDATE SKIP LOCKED`.
7. For returned attempts, decoding, committed-pair validation, and a post-lock `T[1]`.
8. A separately clock-gated attempt mutation followed by required job mutation.
9. Summary exposure only after complete commit and release.

The candidate query requires current ACTIVE attempts, RUNNING jobs, and expiry at/before selection time. A join may select candidate jobs, but locks only the job rows at that stage.

| Observation                                 | Required behavior                            |
| ------------------------------------------- | -------------------------------------------- |
| No candidates                               | `RecoverySummary(0, 0)` after commit/release |
| Attempt `SKIP LOCKED` lookup returns zero   | Skip; no writes or summary increment         |
| Returned pair is malformed or contradictory | Invariant failure; roll back entire batch    |
| Valid pair no longer eligible               | Skip; no guessed resolution                  |
| Expired current ACTIVE below limit          | INTERRUPTED with null code; job QUEUED       |
| Expired current ACTIVE at limit             | EXHAUSTED with null code; job FAILED         |
| Guarded recovery UPDATE returns zero        | Skip; no job write or summary increment      |
| Required mutation evidence fails            | Roll back entire batch; no partial summary   |

Summary counts accumulate only from fully validated attempt/job transition pairs.

There is no refill loop, hidden batch maximum, default batch size, factory invocation, capacity check, or generation increment.

`SKIP LOCKED` intentionally supplies an incomplete observation suitable for queue consumers. Scripted absence must not be described as proof of physical absence or global consistency. [PostgreSQL SELECT locking](https://www.postgresql.org/docs/current/sql-select.html)

## C8 — Committed consistency and replay

### Stored shape and association

Every observed pair requires:

```
A.job_id == J.job_id
1 <= A.attempt_number <= J.attempt_count <= J.attempt_limit
```

Additionally:

- ACTIVE must be current.
- INTERRUPTED and RETRYABLE_FAILURE require number below limit.
- EXHAUSTED requires number equal to limit.
- Every non-null code fully matches `[a-z][a-z0-9_]{0,63}`.

Attempt shapes:

| Status               | Expiry   | Finish   | Code     | Absolute ordering                     |
| -------------------- | -------- | -------- | -------- | ------------------------------------- |
| ACTIVE               | Required | Null     | Null     | heartbeat ≥ start; expiry > heartbeat |
| INTERRUPTED          | Null     | Required | Null     | heartbeat ≥ start; finish ≥ heartbeat |
| RETRYABLE_FAILURE    | Null     | Required | Required | Same resolved ordering                |
| TERMINAL_FAILURE     | Null     | Required | Required | Same resolved ordering                |
| Classified EXHAUSTED | Null     | Required | Required | Same resolved ordering                |
| Expiry EXHAUSTED     | Null     | Required | Null     | Same resolved ordering                |

Observed job-state/count combinations also follow the approved lifecycle:

- QUEUED requires count below limit.
- RUNNING requires positive count.
- FAILED produced by this Worker boundary requires positive count.
- CANCELLED may have count zero or positive count.
- Successful COMPLETED production is deferred; it is not introduced here.

### Complete committed-pair table

`Current` means `A.attempt_number == J.attempt_count`; `Historical` means smaller.

Legend:

- **Valid**: compatible committed pair, subject to all shape/count rules.
- **Cancelled**: compatible cancellation boundary; grants no Worker mutation authority.
- **Future**: historical compatibility with approved future completion, not a Slice 3 producer or successful-finalization specification.
- **Invariant**: contradictory under this contract.

| Attempt relation/status         | QUEUED    | RUNNING   | FAILED    | CANCELLED | COMPLETED |
| ------------------------------- | --------- | --------- | --------- | --------- | --------- |
| Current ACTIVE                  | Invariant | Valid     | Invariant | Cancelled | Invariant |
| Current INTERRUPTED             | Valid     | Invariant | Invariant | Cancelled | Invariant |
| Current RETRYABLE_FAILURE       | Valid     | Invariant | Invariant | Cancelled | Invariant |
| Current TERMINAL_FAILURE        | Invariant | Invariant | Valid     | Invariant | Invariant |
| Current classified EXHAUSTED    | Invariant | Invariant | Valid     | Invariant | Invariant |
| Current expiry EXHAUSTED        | Invariant | Invariant | Valid     | Invariant | Invariant |
| Historical ACTIVE               | Invariant | Invariant | Invariant | Invariant | Invariant |
| Historical INTERRUPTED          | Valid     | Valid     | Valid     | Cancelled | Future    |
| Historical RETRYABLE_FAILURE    | Valid     | Valid     | Valid     | Cancelled | Future    |
| Historical TERMINAL_FAILURE     | Invariant | Invariant | Invariant | Invariant | Invariant |
| Historical classified EXHAUSTED | Invariant | Invariant | Invariant | Invariant | Invariant |
| Historical expiry EXHAUSTED     | Invariant | Invariant | Invariant | Invariant | Invariant |

Derivation and limits:

- Current interruption/retry requeues. RUNNING requires a later fresh claim and therefore a larger count.
- QUEUED may subsequently become CANCELLED without another claim. The resolved attempt remains valid history.
- Terminal failure makes FAILED, which has no approved transition back to claiming. A historical TERMINAL_FAILURE cannot legitimately precede a later generation.
- EXHAUSTED is at the immutable limit; a later generation would also violate the count bound.
- Historical interruption/retry may precede later claims, requeues, terminal failure, exhaustion, cancellation, or deferred successful completion.
- A historical row does not, by itself, prove the correctness of an unread newer attempt.
- The table does not invent a successful current-attempt status. Slice 4 must specify its own successful-finalization persistence before such behavior is introduced.
- ACTIVE alongside CANCELLED is non-authoritative; this does not prescribe Slice 6’s cancellation-resolution policy.

### Validation precedence

For required observations:

1. Validate stored layout, type, status shape, and association.
2. Validate committed job/attempt consistency.
3. Classify ordinary stale supplied authority.
4. Classify exact versus conflicting resolution replay.
5. Evaluate remaining ACTIVE eligibility.

Thus malformed or contradictory current state is not converted to `ClaimLost`, C9a unavailable, or a recovery skip merely because another predicate fails.

An absent job is still `ClaimLost` because no pair was observed. An absent **required unfiltered current attempt** is invariant failure, including when the caller supplied an older generation.

### Replay table

After validation, exact replay requires the same complete fence, same supplied code, matching finalizer kind, and current generation.

| Stored resolution             | Exact permitted replay              | Required job state |
| ----------------------------- | ----------------------------------- | ------------------ |
| RETRYABLE_FAILURE below limit | Retryable finalizer, identical code | QUEUED             |
| Classified EXHAUSTED at limit | Retryable finalizer, identical code | FAILED             |
| TERMINAL_FAILURE              | Terminal finalizer, identical code  | FAILED             |

These return `AttemptResolutionAlreadyApplied` with zero writes and zero factory calls. No unexpired lease is required.

The following return `ClaimLost` after valid-state checks:

- Superseded generation.
- Different slot or lease.
- Different code or finalizer kind.
- INTERRUPTED or expiry-produced EXHAUSTED.
- Cancellation after legitimate requeue.

Cancellation after requeue is **valid stored state**, not corruption. It prevents replay from being treated as current QUEUED authority; no cancellation mutation is implemented here.

### Transactional intermediate states

The committed-pair table applies to authoritative read pairs and the completed mutation pair before commit.

It does **not** reject legitimate intermediate states inside one transaction:

- Fresh claim may update the job before inserting its ACTIVE attempt.
- Finalization/recovery may resolve the attempt before changing the job.

Validate each returned row against its intended mutation and preserved fields, then validate the final pair. Failure of the second write or its evidence rolls back the first. No intermediate pair escapes as an outcome.

## C9 — Approved contention extensions

**C9a and C9b are separately and explicitly approved extensions to Planning-H’s recognized failure conditions.** Approval includes their exact recognition predicates, rollback/release obligations, factory counts, cleanup precedence and prohibition of internal retry. This supplement records those extensions without rewriting the protected Planning-H construction contract.

### C9a — Valid resolved replacement of a slot locator

Concurrent scenario: `L` observes ACTIVE ownership; another authorized transaction resolves it while this claim waits for the job lock. Under Read Committed, later statements can observe that committed replacement. [PostgreSQL transaction isolation](https://www.postgresql.org/docs/current/transaction-iso.html)

Recognition requires **all** of:

1. Exactly one valid `L` from the specified ACTIVE/supplied-slot predicate.
2. Required locked `J[1]`, with `J.job_id == L.job_id`.
3. Required unfiltered locked locator `A[1]`, with PK exactly equal to `L`.
4. Locator `A.worker_slot_id` equals the supplied slot.
5. Locator `A` is validly resolved, not ACTIVE.
6. `A.attempt_number <= J.attempt_count`.
7. The resolved locator pair satisfies C8:
   - If current: INTERRUPTED/retry with QUEUED or compatible CANCELLED, or terminal/exhausted with FAILED.
   - If historical: only INTERRUPTED/retry can legitimately precede later generations.
8. If historical, a separate required unfiltered current-attempt read succeeds and that current pair also passes shape, association, and committed consistency.
9. No required observation is missing, malformed, or contradictory.

`L` does not contain the old lease or timestamps. Tests cannot claim to compare those unavailable original fields. Source/live evidence must establish that authorized resolution preserves immutable attempt authority.

Approved recognized behavior:

- Roll back and release.
- Raise the fixed Worker unavailable error.
- Factory calls: **0**.
- No writes, rediscovery loop, second job selection, fabricated claim, or internal retry.

Missing required rows, wrong locator association, changed slot, historical terminal/exhausted status, or contradictory current state produce invariant failure—not C9a.

### C9b — Fresh ACTIVE-slot INSERT collision

Concurrent scenario: two transactions observe a free slot and select different jobs. The ACTIVE-slot unique index arbitrates their fresh INSERTs.

Recognition requires **all** of:

1. The exception occurs at `await connection.execute(...)` for the **fresh attempt INSERT only**.
2. It is an actual Psycopg `UniqueViolation`.
3. Its diagnostic field satisfies exactly:

```
diag.constraint_name == "analysis_job_attempts_active_worker_slot_id_idx"
```

No message matching or arbitrary SQLSTATE duck typing is used. Installed Psycopg defines `UniqueViolation` under `IntegrityError`, not `OperationalError`, and exposes nullable diagnostic constraint names. [Psycopg errors](https://www.psycopg.org/psycopg3/docs/api/errors.html)

Recognition does not apply to:

- Missing, null, or wrong constraint diagnostics.
- Other uniqueness/integrity failures.
- Job UPDATE, fetching, cursor closure, transaction completion, or connection release.
- Factory-raised exceptions, including an otherwise matching `UniqueViolation`.
- Non-driver objects with matching-looking fields.

Approved recognized behavior:

- Roll back the complete claim transaction, including its tentative job/count update.
- Release resources.
- Raise the fixed Worker unavailable error.
- Factory calls remain **1**.
- Discard the uncommitted lease.
- A later caller invocation starts with normal persisted-slot discovery.
- No savepoint retry, internal retry, or alternative contention design is introduced.

Unrecognized exceptions follow Planning-H’s unchanged boundary classification; unrelated integrity/programming exceptions are not broadly translated.

### Shared error and cleanup requirements

For both recognized C9 branches:

1. Leave the recognition handler without propagating the raw driver exception.
2. Establish the fixed sanitized unavailable primary **before cleanup-primary handling**.
3. Roll back and release using that primary.
4. Ordinary cleanup failure preserves it.
5. Fatal cleanup failure preserves fatal identity and takes the sanitized primary as cause.

No raw driver cause/context, diagnostic attributes, messages, or SQL parameters are attached to the public error.

Both extensions are explicitly approved. Any replacement behavior requires an explicit separate decision. RED-H must not silently invent a retry or substitute outcome.

## C10 — RED-H coverage and evidence limits

The deterministic Worker repository contract remains:

```
tests/test_analysis_job_worker_repository_contract.py
```

Scripted boundaries supply raw observations and record effects. They do not select policy, increment counters, resolve attempts, interpret SQL, simulate locks, or manufacture outcomes.

Required coverage includes:

| Area              | Required characterization                                                                                                                              |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Construction      | Exact exports/error identity, constructor, annotations, timeout order/ranges, zero pool/factory effects                                                |
| Rows              | Every layout position, type/nullability, tuple length, singleton duplicates, missing required rows                                                     |
| Consistency       | Every C8 current/historical cell and state/count condition                                                                                             |
| Absence           | Missing job versus missing current attempt; stale generation with valid versus corrupt current state; locator-required absence; recovery lock-skipping |
| Authority         | Independently vary all four components; generation-two preservation; same-slot persisted retry                                                         |
| Factory           | Placement/counts, UUID identity, nil/subclass/async rejection, unchanged exception identity                                                            |
| Time              | Fold-crossing valid/invalid/equal cases, extreme dates, exact duration, preserved row objects/offsets/folds                                            |
| Mutation evidence | Zero/duplicate/wrong-job/wrong-generation results, changed policy, wrong status/code, timestamp preservation and old-expiry relations                  |
| Replay            | Exact retry/terminal replay; conflicts; supersession; cancellation after requeue                                                                       |
| Recovery          | Exact limit, no refill, skip handling, guarded zero, counts only from complete validated pairs                                                         |
| Atomicity         | Failure after first write rolls back all writes; no partial outcome/summary                                                                            |
| Resources/errors  | Cursor closure, transaction exit/release before exposure, narrow classification, sanitized primaries, unchanged fatal precedence                       |
| C9                | Complete positive predicates and one-condition negative cases; no internal retry                                                                       |
| Inactivity        | No runtime composition, claim loop, successful finalization, or outbox                                                                                 |

No node counts or passing results are asserted by this planning document.

### Evidence tiers

| Tier                        | Establishes                                                                                                                                                              | Does not establish                                                                                                          |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| Scripted tuples/effects     | Decoder mapping, validation, supplied parameters, mutation-result checking, factory counts, outcome/error handling and lifecycle observations                            | Actual SQL projections/bind associations, database-clock provenance, locks, uniqueness arbitration, or PostgreSQL execution |
| Independent source review   | Explicit projections, field-to-placeholder associations, predicates, job-first locking, single captured mutation clock, exact elapsed arithmetic, transaction boundaries | Actual concurrent database behavior                                                                                         |
| Authorized live integration | Real clock/lease gates, transactional rollback, locking, uniqueness arbitration, same-slot races, recovery/reclaim fencing and durable counts                            | Production deployment or activation approval                                                                                |

The current cursor supplies no column-origin metadata. A coordinated SQL/decoder swap cannot be ruled out through tuple fixtures alone.

Source review must remain independent and bounded. Do not conceal this limitation with a general SQL parser, database simulator, stronger invented cursor port, or public decoder API.

## Preserved snapshots and later obligations

Admission’s ordered projection remains:

```
job_id, idempotency_lookup, request_fingerprint,
video_id, player_id, video_reference, callback_url,
state, accepted_at,
analysis_version, attempt_limit, attempt_count
```

Fresh admission retains the original bound fields followed by:

```
candidate.analysis_version, candidate.attempt_limit, 0
```

Acceptance time remains database-generated.

- `NewAnalysisJob` and `AnalysisJob` receive required version/limit in Planning-F’s exact field order.
- Neither gains public `attempt_count`.
- `ClaimableAnalysisJob` remains unchanged.
- Identical lookup/fingerprint retries retain authoritative stored policy.
- Snapshots do not alter Fingerprint V1 or HMAC framing.
- `HttpUrl` subclasses remain accepted at canonicalization; stored callback strings remain exact built-in canonical strings.
- Prior validation, lifecycle, capacity, idempotency, cleanup, exception, fingerprint, and HMAC assertions remain intact.

Later obligations remain separate:

- Real strict-Mypy composition witness.
- Separately authorized integration-fixture snapshot alignment.
- Separate integration catalog/schema reconciliation before new live evidence.
- Live ownership/recovery races.
- Writer-blocking downgrade: wait for an in-flight writer, refuse after its commit, preserve its row and policy/count data.
- Slice 4 successful fenced finalization/results/outbox, applicable later slices, Slice 10 evidence, and explicit Slice 11 activation authorization.

Fixture policy examples are not production defaults. No schema, migration, grant, API, cancellation operation, successful-finalization operation, or runtime behavior is added.

## Recorded explicit approval and authorization boundary

Explicit project-owner approval covers **C1–C10 as concretely specified here**, including:

- Required current-attempt validation before stale-generation classification.
- Additional current-pair validation after historical locator discovery.
- The complete committed-state/replay table.
- Decoder-verifiable timestamp bounds versus separate clock-provenance obligations.
- **C9a and C9b individually as contract extensions.**
- C10’s explicit evidence limitations.

This document records explicit approval of the decisions, not authorization to implement them. Documentation-fidelity review is the next gate; RED-H, GREEN-H, integration execution and runtime activation require separate authorization.

The two pre-existing Planning-F Python-fence formatting findings remain separate and untouched. This documentation task authorizes no production, test, migration, configuration or runtime changes.

## Independent review qualifications and authoritative source

The independent review found the corrected decisions eligible for explicit approval; the project owner has now approved C1–C10, including C9a and C9b separately. These qualifications remain part of the documentation boundary:

- Same-slot validity is established at the post-lock observation. Remaining lease time at return is not guaranteed.
- Clock reversal has no added clamping policy.
- Unrecognized `CheckViolation` follows Planning-H’s existing narrow propagation rules.
- Slice 4 must reconcile successful current-attempt representation before extending this inactive boundary.
- Scripted tuples do not establish SQL clock provenance or live PostgreSQL concurrency.
- A real strict-Mypy production composition witness remains required later; none was executed by the planning review.
- Integration-fixture policy values are examples, not production defaults. Fixture alignment, separate integration catalog/schema reconciliation and live database evidence remain deferred and require their applicable authorization.

The authoritative source is the complete designated corrected proposal, not the old two-draft attachment or another copy:

| Source evidence | Verified value |
| --- | --- |
| Input | `E:\Super 7\planning-h-row-contract-corrected-proposal.md` |
| Lines | 842 |
| Bytes | 51,349 |
| SHA-256 | `5de40e499018c46e341971f1b3fc7ef45c7c4be571a6db99fd19427a43497472` |

Only approval-status wording, provenance clarification and these reviewed qualifications differ editorially from that input. Normative layouts, declarations, tables, predicates, operation branches and C1–C10 identifiers remain preserved.

PLANNING-H ROW CONTRACT DOCUMENTED — READY FOR FIDELITY REVIEW
