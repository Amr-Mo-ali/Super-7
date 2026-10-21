# Slice 4 — fenced terminal persistence and callback event creation
**Status: SF-01–SF-08 and OD-01–OD-07 approved by the project owner on 2026-10-13; documentation only.** This is the approved local persistence contract, not authorization for implementation, tests, migration execution, callback interoperability, deployment, or production activation. The SF amendments to the approved Slice 3 boundary and the OD selections below are normative. No durable worker may execute accepted work until a successful finalizer is implemented and separately authorized for activation.

Authority: [ADR-004](../../decisions/ADR-004-analysis-job-lifecycle-and-idempotency.md), [ADR-005](../../decisions/ADR-005-durable-job-storage-and-worker-architecture.md), [ADR-008](../../decisions/ADR-008-durable-job-recovery-and-callback-outbox.md), the [Planning-F worker contract](04-worker-claim-lease-fencing-contract.md), [Planning-H construction contract](05-worker-repository-construction-contract.md), and [Planning-H row contract](06-worker-repository-row-contract.md). The designated external transcription sources were `slice4-contract-draft-v3.md` (55,777 bytes; SHA-256 `13202a5aab4372053ceb7c21ba3f5d643b9a35bdfba935bafee3b2316e852929`) and `slice4-owner-decision-sheet-v2.md` (7,568 bytes; SHA-256 `9fbc867e75fa4edba246842fbed79bc2819ca1f41c6305a1efc2309b3464babd`). This contract is self-contained; those external drafts are not repository-tracked authority needed to interpret it.

## Decision summary and authority

| ID | Approved Slice 4 decision | Authority and material alternative / cost | Deferred gate |
| --- | --- | --- | --- |
| SF-01 | Add a separate success-finalization port using the existing four-part fence; leave all five Worker operations and lease-factory counts unchanged. | Planning-F/C8 establish ownership; combining success into a Worker signature would reopen the approved port. | Implementation and evidence. |
| SF-02 | A validated `AnalyzeResponse` is execution success even when its pipeline status is not `completed`; store that status without inventing ratings. A classified child failure is different. | Current parent behavior; treating `no_players_detected` as an execution failure would change product meaning. | Conversion evidence. |
| SF-03 | Add only `SUCCEEDED` as a resolved attempt status and current `SUCCEEDED`/`COMPLETED` pairing. | Slice 3 has no successful attempt representation; a job-only `COMPLETED` write would be inconsistent. | Migration and live evidence. |
| SF-04 | Persist a versioned, explicit, immutable successful-result projection and callback snapshot made together from one validated input; omit debug/local artifacts and free-form diagnostics. Use strict JSON and retain first-commit measurements. | ADR-008 requires bounded metadata rather than diagnostic artifacts. Full `AnalyzeResponse` retention could store paths and uncontrolled data and is not selected. | Projected-size measurement and deployment model catalog (OD-01/02). |
| SF-05 | Create one `PENDING` outbox row with every `COMPLETED`/`FAILED` job, with PostgreSQL-generated UUID; retain today's callback body as baseline and add the event identity to eventual transport, without extra job-ID fields or an envelope. | ADR-004/005/008 require event identity and atomicity. | Apex conformance and delivery authorization (OD-06). |
| SF-06 | Exact replay compares the complete fence, substantive stored result/callback JSONB snapshots, version metadata and terminal kind; exclude exactly two measurement fields while leaving first-commit values untouched. Return the original event UUID and finish time without writing. | C8 currently covers only failure-code replay; OD-05 selects the precise exception. | Comparator evidence. |
| SF-07 | Stop incompatible writers, lock jobs then attempts before upgrade preflight/DDL; preserve valid nonterminal data and refuse unrepresented terminal data. Downgrade refuses any Slice 4 terminal/event/success data. | Slice 3's empty-apply rule does not transfer; preflight without locks races a writer. Strong table locks pause readers too. | Migration execution/runbook evidence; `5s` per lock wait is selected (OD-04). |
| SF-08 | Extend the existing terminal-failure, retryable-at-limit and recovery-at-limit transactions to insert the failure terminal row and one event. Preserve recovery's one-transaction batch. | ADR-004/005/008 require terminal failure callbacks. This intentionally amends Slice 3 Worker terminal behavior without changing its five signatures or nonterminal behavior. | Classified-code composition, Apex conformance and live evidence (OD-03/07). |

Accepted authority remains: PostgreSQL is the Super-7 source of truth; terminal state/result/event commit atomically; callback failure cannot rewrite analysis state; one job has one logical result; stale authority cannot finalize; accepted null ratings stay null; eventual delivery is at least once. The current repository implements only Slice 3 and is inactive. No V1 external wire envelope is selected. Cancellation, sending, dispatcher leasing/retry, result-query API, coordinator, route cutover, and process-count changes are outside this contract.

## Public port, conversion, and outcomes (SF-01, SF-02, SF-04, SF-06)

Approved declarations for `src/services/analysis_job_finalization.py` (exact future public shape; not yet implemented):

```python
@dataclass(frozen=True, slots=True)
class FinalizableAnalysisResult:
    result_schema_version: Literal["analysis_result_v1"]
    result_json: bytes  # frozen, strict UTF-8 projected JSON object
    callback_schema_version: Literal["analysis_callback_snapshot_v1"]
    callback_json: bytes  # frozen current body projection, without transport additions
    analysis_version: str  # child-resolved version
    model_version: str  # approved path-free model identity
    processing_duration_ms: int  # exact nonnegative int, BIGINT range


@dataclass(frozen=True, slots=True)
class AnalysisJobCompleted:
    authority: AttemptAuthority
    finished_at: datetime
    callback_event_id: UUID


@dataclass(frozen=True, slots=True)
class AnalysisJobCompletionAlreadyApplied:
    authority: AttemptAuthority
    finished_at: datetime
    callback_event_id: UUID


class AnalysisJobSuccessFinalizationRepository(Protocol):
    async def finalize_success(
        self, *, authority: AttemptAuthority, result: FinalizableAnalysisResult
    ) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost: ...


async def finalize_analysis_job_success(
    *,
    repository: AnalysisJobSuccessFinalizationRepository,
    authority: AttemptAuthority,
    result: FinalizableAnalysisResult,
) -> AnalysisJobCompleted | AnalysisJobCompletionAlreadyApplied | ClaimLost: ...


AnalysisJobFinalizationPersistenceUnavailable = AnalysisJobWorkerPersistenceUnavailable
AnalysisJobFinalizationPersistenceInvariantError = AnalysisJobWorkerPersistenceInvariantError
```

The aliases are identities, not new exception categories. Returned UUIDs must be exact built-in, nonnil `UUID` values and timestamps must be aware. The wrapper and repository validate `authority` and `result` before pool interaction: exact field types; known schema tags; strict UTF-8 JSON **objects** with duplicate object keys rejected at every depth, finite numbers representable by PostgreSQL `jsonb`, no unsupported JSON values, valid Unicode scalar text and no NUL; approved field/variant rules; full-match analysis/model version syntax; exact nonnegative duration within PostgreSQL `BIGINT`; `result.analysis_id == str(authority.job_id)`; and, for `CompletedResponse`, embedded analysis/version/model identities agreeing with the child envelope after the approved path-free model conversion. A serializer may use stable key order/compact separators, but no canonical-byte identity is required or used for replay. A malformed input raises `TypeError`/`ValueError` without database I/O. It is not translated into a persistence invariant. Stored malformed rows, returned evidence, or associations raise the existing fixed sanitized invariant error. Pool/connection/statement/lock unavailability uses the existing fixed unavailable error. Unexpected programming and fatal exceptions retain identity; approved primary/cleanup precedence and complete resource release apply.

**Approved conversion boundary (OD-07):** one pure conversion call accepts a validated `ChildAnalysisSuccess`, the same validated `AnalyzeResponse`, and its `WorkerClaim` (including immutable `job.video_id`, `job.player_id`, `job.analysis_version` and `job_id`). Strictly decode the original child `response_json` with duplicate-key rejection **before** model coercion can erase duplicates; validate child/claim/response identity, materialize defaults once, and produce **both** frozen JSON snapshots synchronously before the first `await`. Separately supplied result and callback bytes are not a trusted pair. The call uses one shared deterministic presentation mapper and verifies `callback_json = callback_projection(result_json, job.video_id, job.player_id, str(job_id))` under the approved serialization/JSONB semantics before persistence. The current `_callback_payload` is synchronous/pure with respect to I/O but lives in an API module and uses Pydantic plus rating presentation. Move its pure mapping into a shared presentation adapter while preserving its output and having the legacy route call that adapter; the conversion adapter uses the same mapper. Neither the domain nor the finalization port imports HTTP/Pydantic types. This is mapping reuse, not route activation or a public API change. The port transports only frozen bytes; the repository validates strict shape, schema tags, job/attempt association and stored invariants, but does **not** recalculate CV analysis or ratings. Cross-snapshot semantic validation belongs to the conversion boundary and its contract tests; callers bypassing it are invalid. Failure/exhaustion snapshots are produced by a separate pure terminal mapper from the locked job identity and approved safe failure code/origin, never from a success result.

Approved future Psycopg constructor: `PsycopgAnalysisJobFinalizationRepository(*, pool, connection_acquire_timeout_seconds: float, operation_timeout_ms: int, lock_timeout_ms: int)`. Use the same structural pool/connection/cursor contract, validation order, `operation_timeout_ms > lock_timeout_ms`, transaction-local settings, one transaction per call, no default timeouts, no internal retry, and no event-ID/lease-ID factory. The five existing Worker signatures and its pinned `lease_id_factory` constructor remain unchanged; terminal failure and recovery call that factory zero times.

## Result and callback snapshots (SF-02, SF-04–SF-06)

`analysis_result_v1` is a **projection**, not a new score formula or a promise to return the entire legacy `AnalyzeResponse` later. The conversion adapter first validates the complete child response using the existing schema, then materializes model defaults, then selects the following JSON fields. JSON `null` is retained as `null`, never changed to zero, false, or absence. Array order is retained. The projection is frozen before I/O.

| Response variant | Retained result fields | Deliberately omitted/replaced |
| --- | --- | --- |
| `CompletedResponse` | `analysis_id`, `status`, `video`, `selected_player`, `tracking`, `features`, `interaction_analysis`, `technical_event_analysis`, `pass_detection`, `shot_detection`, `scores`, `player_rating_summary`, `result_availability`, `unavailability_reason`, `analysis_version`, safe `model_version`, `processing_time_ms`, `pipeline_state`, `algorithm_versions`, `quality_gates`, the typed `analysis_metadata` subset and diagnostic subset below. | `debug_artifacts`, arbitrary `metadata`, other `analysis_metadata` leaves, warnings, raw diagnostic maps/lists, and per-stage `timing`. |
| `AmbiguousResponse` | `analysis_id`, `status`, `selected_player: null`, `candidate_count`. | Arbitrary `metadata` and warnings. |
| `NonCompletedResponse` | `analysis_id`, `status`, `selected_player: null`, `candidate_count`, `pipeline_state`, `quality_gates`, and the diagnostic quality subset below. | Arbitrary `metadata`, warnings and raw diagnostic maps/lists. |

The diagnostic subset contains the six required count fields (`frames_processed`, `frames_with_player_detections`, `total_person_detections`, `tracks_created`, `valid_candidate_tracks`, `ball_detections`) and only `ball_analysis_quality`, `interaction_analysis_quality`, `movement_analysis_quality`, and `technical_event_analysis_quality`. These are the diagnostic values the current public rating/callback mapping actually consumes; candidate-event evidence, rating values, availability and nulls stay in their typed result fields. The conversion adapter validates retained typed string leaves as JSON-compatible text and excludes raw free-form warnings, metadata, local paths, artifact references, secrets and debug filenames; it never persists debug bytes or full diagnostic arrays. For a `CompletedResponse`, retain only `result_json.analysis_metadata.video_hash` (lowercase 64-character SHA-256 hex), `.configuration_profile` and `.pipeline_version` (nonempty path-free tokens matching `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`), and `.git_commit` (null or lowercase 40-character Git hex); reject invalid retained values before I/O rather than silently rewriting them. Drop `analysis_metadata.timestamp`, raw `analysis_metadata.model_versions` and every other free-form metadata leaf. `algorithm_versions` and rating/scorer versions remain in the typed result, but `result_json.model_version` **and** `result_json.algorithm_versions.player_detection` must use the same approved path-free model identity: the latter currently embeds the raw model-path string too. Preserve the remaining algorithm-version leaves without inventing versions. The approved executable mapping is a configured logical ID for the exact selected player-model/ball-model pair; verify the child-reported `model_path+ball_model_path+bytetrack` identity against the configured pair and reject unknown/mismatched pairs before I/O. A checksum-based mapping is an alternative requiring separately approved file-verification work, not assumed here. Missing safe identity prevents finalization rather than silently using an invented version. This exact projection and version mapping are approved; concrete logical IDs, path-pair catalog values and deployment verification remain composition prerequisites.

**Success consistency (OD-07).** The current callback builder needs `analysis_id` and pipeline `status`; for a completed result it also needs the retained selected player, video/tracking/features/interaction and event evidence, scores and rating summary, result availability/reason, pass/shot data and the retained diagnostic counts/quality subset used by `public_rating_v2`, event arbitration and detailed ratings. It additionally needs immutable `video_id` and `player_id` from `WorkerClaim.job`; those are **not** in the result projection. The omitted warnings, free-form metadata, raw diagnostic collections, debug artifacts, per-stage timing and analysis metadata do not populate fields of today's callback body. For noncompleted variants, the present `public_rating_v2` helper accepts warnings/metadata but `_callback_payload` does not use their failure `reason`, `warnings` or `metadata`; the shared mapper must use neutral reconstruction defaults only for those omitted inputs and assert identical callback output. The minimal arrangement is to project/validate the result once, pass that projection plus the two job IDs to the shared pure callback mapper, and freeze its output in the same conversion call; no extra result fields or independently trusted callback bytes are needed. Require `result_json.analysis_id = callback_json.request_id = str(authority.job_id)`, `callback_json.video_id = claim.job.video_id`, `callback_json.player_id = claim.job.player_id`, and `callback_json = callback_projection(result_json, claim.job.video_id, claim.job.player_id, str(authority.job_id))` as JSONB-equivalent objects, including nulls and array order. `COMPLETED` durable state may still carry a noncompleted pipeline callback status as described below. A conversion contract assertion for each response variant compares the stored outbox payload with that deterministic projection; this tests the pure boundary without making the repository recompute ratings.

The internal callback snapshot is a separate JSON object because a compact public projection is not the same as the durable result. It stores the **current** `CallbackPayload`/`FailedCallbackPayload` body projection (including its current snake-case and alias serialization, statuses, summary, ratings, detailed/overall, events, nulls, availability/player fields, and safe failure error), without `jobId` or `callbackEventId`. Those identities live in the locked job/outbox columns; the approved event identity is added to eventual transport. The builder must produce a snapshot whose request/video/player IDs match the locked job and whose status follows the existing mapper for the projected pipeline status. A successful `NonCompletedResponse(status="failed")` can therefore have callback `status: "failed"` while the durable job is `COMPLETED`; this is an existing pipeline/execution distinction, not a terminal `FAILED` job. The snapshot contains neither `processing_duration_ms` nor `processing_time_ms`; today's callback mapper reads neither. It has no callback URL, authentication material, video bytes, local paths, or debug artifacts. This intentional two-representation design avoids recomputing a callback under a later scoring/mapping release; it is not a generic deduplication system. No callback may be sent in Slice 4.

Replay equality is evaluated **inside PostgreSQL**, not by Python equality of decoded JSON: pass the strictly decoded/frozen UTF-8 JSON documents as bound `jsonb` parameters and compare stored `jsonb` values there. The adapter materializes declared defaults before projection; an absent optional source field that becomes a model default therefore matches the explicit default, while a genuinely omitted projected key differs from a projected JSON `null`. Object-key order/whitespace do not matter; array order does. JSON numeric equality follows PostgreSQL `jsonb` (so `1` and `1.0` compare equal), but JSON booleans are distinct from numbers. Reject duplicate object keys at every depth, nonfinite/out-of-range numeric values, invalid UTF-8/Unicode, NUL and schema/field violations before I/O; PostgreSQL 16 documents [JSONB normalization and numeric limits](https://www.postgresql.org/docs/16/datatype-json.html). Serializer spacing and key order are not acceptance conditions. Compare the frozen projected input, not original child JSON bytes, a mutable model, or an admission fingerprint.

**Approved measurement replay (OD-05).** The child-envelope `processing_duration_ms` measures elapsed execution through child serialization in `process_entrypoint.py`; retained `result_json.processing_time_ms` is the pipeline-reported time from `routes.py`. They are different measurements, and today's callback mapper reads neither. The approved rule stores both first-commit values unchanged and excludes **exactly** `R.processing_duration_ms` and top-level `R.result_json.processing_time_ms` from replay comparison on **both** supplied and stored sides. For success the JSON expression is `(r.result_json - 'processing_time_ms') = (%s::jsonb - 'processing_time_ms')` with the supplied result bound to `%s`; separately compare the full callback `o.payload_json = %s::jsonb`. For noncompleted variants without the field, object-key subtraction is a no-op. No other measurement or version field is exempt: compare the full fence, terminal kind, result/callback schema tags, analysis/model versions and all other retained result and callback content. Since callback snapshots contain neither measurement, their full comparison remains coherent. Replay returns the first event/finish and never updates either measurement or either snapshot. A fresh execution with changed substantive content is **not** promised acceptance after an ambiguous commit; it is a conflict, not overwrite permission.

The separate replay observation `Q` is **one row with one column** named `equivalent`, exact built-in Python `bool`, from a bound-parameter SQL `SELECT` over the already-required R/O pair by job ID; use `fetchall()` and require a one-element tuple/list in a singleton outer tuple/list. A missing/duplicate/wrongly typed Q row is the existing fixed invariant error, never `ClaimLost`. On success, Q evaluates the two JSONB expressions above using the supplied frozen result/callback input. On failure/exhaustion it evaluates `o.payload_json = %s::jsonb` (there is no success result), binding the expected callback JSONB **derived internally** by the versioned pure failure mapper, not a callback argument to a Worker method and not a copy of stored O. Schema tags, terminal origin/code, full authority and version fields remain explicit scalar comparisons against the already validated J/A/R/O observations before successful replay. Q does **not** add columns to R/O or authorize an extra R/O read on a nonterminal path. All JSONB parameters are bound, never interpolated. Exact false after sound stored-state validation means conflicting replay and `ClaimLost`.

### Size evidence and approved policy (OD-01)

Existing `server-*-response.json` samples were read for shape/size only; all ten are `completed` examples and the two locations contain the same five byte sizes. No payload is reproduced here:

| Sample filename (observed in two matching locations) | Bytes per file |
| --- | ---: |
| `server-calibration-response.json` | 13,356 |
| `server-instrumentation-response.json` | 13,356 |
| `server-exposed-diagnostics-response.json` | 17,261 |
| `server-dribble-response.json` | 17,605 |
| `server-v03-response.json` | 43,537 |

These are **full legacy response** samples, not measured projected-result/outbox sizes, a representative video-length distribution, or a maximum legitimate output. OD-01 approves **no application-level byte rejection in Slice 4 for the explicitly projected result and callback snapshot** and accepts the storage/WAL/transport risk. Measure serialized projected bytes, database size, TOAST/WAL growth and callback payload size in disposable tests and pre-activation validation; ADR-008's exclusion of unbounded diagnostic artifacts still applies. Do not invent a cap, oversize outcome or truncation of ratings, events or null evidence. PostgreSQL's physical limit is not a product policy.

### Current versus approved eventual wire (OD-06)

| Boundary | Current sent body/headers | Approved Super-7 additive transport; delivery deferred |
| --- | --- | --- |
| Success/non-failed result | `request_id`, `video_id`, `player_id`, `status`, `summary`, `ratings`, `overall`, `detailed`, `events`, optional `error`, `resultAvailability`, `unavailabilityReason`, `player`, `overallConfidence`; `status` may be `COMPLETED` or a noncompleted pipeline value. | Preserve every existing field/value/projection and add `callbackEventId = str(event_id)` only. No wrapper or new rating calculation. |
| Failure | `request_id`, `video_id`, `player_id`, `status: "failed"`, empty summary/ratings/events, optional overall, safe `error`; no `detailed`. | Preserve that shape; add `callbackEventId` and the approved safe code/message mapping below. `resultAvailable` remains false in durable job metadata, not an invented rating. |
| Headers | `Content-Type: application/json`, `Accept: application/json`. | Preserve both and add `X-Super7-Callback-Event-Id` with the exact stored UUID. No schema wrapper, signature or authentication invented here. |

ADR-004 approves stable `callbackEventId` in the callback payload **and** header; OD-06 selects the exact spelling `X-Super7-Callback-Event-Id`. ADR-004 makes `jobId` canonical and the migration `analysisId` alias equal to it. The current route sets callback `request_id = result.analysis_id` (failure: `job.analysis_id`); the approved conversion requires `request_id = str(job_id)`, so the alias does not create a second identity. Preserve the existing callback body projection and add only `callbackEventId` to the eventual body and the selected Event-ID header. Do not add an extra `jobId` body field, job-ID header, V1 envelope or schema-version header in this contract. This mapping is not currently sent or Apex-approved. Apex must prove parsing, duplicate-event acknowledgement, authentication/replay and non-scoring outcome vectors before real delivery. The internal `analysis_callback_snapshot_v1` tag is not a public wire schema version.

## Attempt/job consistency and observations (SF-01, SF-03, SF-06, SF-08)

Extend `AttemptStatus` and the Slice 3 attempt CHECK with `SUCCEEDED`: `lease_expires_at IS NULL`, `safe_failure_code IS NULL`, `finished_at IS NOT NULL`, and `started_at <= heartbeat_at <= finished_at`. Preserve all other status shapes, one-active-job/slot indexes, immutable policy/count and existing J/A/L/T projections. `SUCCEEDED` is legal only as the **current** attempt of `COMPLETED`; a historical `SUCCEEDED` is contradictory. Historical `INTERRUPTED`/`RETRYABLE_FAILURE` below the limit may precede it. `COMPLETED` with current `ACTIVE`, no current attempt, or another current resolution is contradictory. `FAILED` remains current `TERMINAL_FAILURE` or classified/expiry `EXHAUSTED`; `EXHAUSTED` is an attempt status, not a second event or a job status.

Exact C8 amendments (all unlisted cells and shape/count rules remain unchanged):

| Relation/status | QUEUED | RUNNING | FAILED | CANCELLED | COMPLETED |
| --- | --- | --- | --- | --- | --- |
| Current `SUCCEEDED` | Invariant | Invariant | Invariant | Invariant | Valid, subject to terminal/outbox observation where prescribed |
| Historical `SUCCEEDED` | Invariant | Invariant | Invariant | Invariant | Invariant |
| Historical `INTERRUPTED` | Valid | Valid | Valid | Cancelled | Valid history; current row still must be read where required |
| Historical `RETRYABLE_FAILURE` | Valid | Valid | Valid | Cancelled | Valid history; current row still must be read where required |

Replace C8's “COMPLETED production deferred” and two “Future” cells with the above; authorize the Worker decoder to recognize the new shape. Validation precedence remains layout/type/association, then committed job/attempt pair, then ordinary stale authority, then exact/conflicting replay, then remaining ACTIVE eligibility. Where terminal observations are prescribed, validate those **after** the J/A pair and **before** stale/replay/C9a classification. No operation may turn a missing required current row into `ClaimLost`. Intermediate states inside one transaction (attempt resolved before job transition, job changed before terminal insertion) are allowed only until the complete final pair is checked before commit.

Use separate immutable observations, **not** a silent join or enlargement of the approved J (10), A (10), L (2), T (1) layouts:

| Row | Exact SELECT/RETURNING order and Python types | Cardinality/association |
| --- | --- | --- |
| `R` terminal | `job_id` UUID; `attempt_number` exact int; `terminal_kind` exact str; `finalized_at` aware datetime; `terminal_schema_version` exact str; `result_schema_version` nullable exact str; `result_json` nullable decoded JSON object; `model_version` nullable exact str; `processing_duration_ms` nullable exact int; `safe_failure_code` nullable exact str; `failure_origin` nullable exact str. | PK `job_id` predicate, no state filter, no `LIMIT 1`; exactly one for an observed terminal pair, zero for a nonterminal pair. Match J/A job/count/status/finish and stored version rules. |
| `O` outbox | `callback_event_id` exact nonnil UUID; `job_id` exact nonnil UUID; `payload_schema_version` exact str; `payload_json` decoded JSON object; `delivery_state` exact str; `created_at` aware datetime; `next_attempt_at` aware datetime. | Unique `job_id` predicate, no delivery-state filter, no `LIMIT 1`; exactly one for an observed terminal pair, zero for a nonterminal pair. Match R/J, payload identities/kind and `created_at = R.finalized_at`. Delivery state may subsequently change without changing analysis state. |

Use `fetchall()` and exact tuple/list/cardinality checks as for J/A; reject duplicate/malformed/missing required rows as the existing invariant error. Read R then O under already-held job/attempt locks only at the following operation-specific points. “Pre-existing” means SELECT existing rows before classification/mutation; “inserted” means validate R/O from their own INSERT `RETURNING` evidence in the same transaction, not an extra SELECT. All other J/A/L/T layouts and approved nonterminal behavior remain unchanged.

| Operation / observed current pair | Pre-existing R/O read or absence check | Inserted R/O evidence |
| --- | --- | --- |
| `claim_next`: free slot/ordinary queued claim | None; no global R/O audit. | None. |
| `claim_next`: occupied-slot C9a resolved locator, including a historical locator's current replacement | If the **current** J/A pair is `COMPLETED`/`SUCCEEDED` or `FAILED`/terminal resolution, read exactly one R then O and validate before fixed unavailable classification; if current pair is nonterminal, no R/O read. Historical `SUCCEEDED` remains invariant. | None. |
| `renew_lease`: valid active or ordinary stale owner | None; retain J/A/lease predicates and outcomes. | None. |
| `finalize_retryable_failure` below immutable limit: new resolution or exact `RETRYABLE_FAILURE` replay | None; requeue has no terminal/event. If a terminal **current** pair is encountered in its required current-pair classification path, validate existing R/O before stale/conflicting outcome. | None. |
| `finalize_retryable_failure` at limit: new `EXHAUSTED`/`FAILED` | Check both R and O absent before mutation. | Validate singleton R and O `RETURNING` plus final J/A/R/O pair. |
| `finalize_retryable_failure` at limit: exact `EXHAUSTED` replay or other observed terminal current pair | Read exactly one R and O before already-applied versus conflicting/stale classification; validate supplied attempt code `x`, exhaustion terminal code/origin and Q against the internally derived callback for exact replay. | None. |
| `finalize_terminal_failure`: new `TERMINAL_FAILURE`/`FAILED` | Check both R and O absent before mutation. | Validate singleton R and O `RETURNING` plus final J/A/R/O pair. |
| `finalize_terminal_failure`: exact replay or other observed terminal current pair | Read exactly one R and O before already-applied versus conflicting/stale classification; match supplied terminal code `x`, origin and Q against the internally derived callback for exact replay. | None. |
| `finalize_success`: new `SUCCEEDED`/`COMPLETED` | Check both R and O absent before mutation. | Validate singleton R and O `RETURNING` plus final J/A/R/O pair. |
| `finalize_success`: exact replay or other observed terminal current pair | Read exactly one R and O before already-applied versus conflicting/stale classification; use Q only for a sound success replay candidate. | None. |
| `recover_expired`: selected active below limit or skipped candidate | None; interruption/requeue has no terminal/event. | None. |
| `recover_expired`: selected active at limit | Check both R and O absent before terminal mutation. | Validate singleton R and O `RETURNING` plus final J/A/R/O pair, within the unchanged whole-batch transaction. |

Where an operation requires a current-pair terminal observation, `COMPLETED` or `FAILED` without its consistent R/O is invariant failure, never ordinary unavailable/`ClaimLost`. Do not read R/O merely because a historical attempt is terminal or as a global audit. The specified R/O validation follows J/A pair validation and precedes ordinary stale/replay/C9a classification; Q is evaluated only for exact replay candidates after sound stored-state validation.

C9a's exact amendment is to add `SUCCEEDED`/`COMPLETED` as a valid resolved **current** locator pair, and to require terminal R/O observations for that pair and for any terminal current replacement after a historical locator. Preserve L's lack of original lease/timestamps, job-before-attempt locking, all prior predicates, zero factory calls, rollback/release, and no rediscovery/retry. Historical `SUCCEEDED` remains invariant. This does not broaden C9b or turn an ordinary stale owner into an audit.

## Approved persistence schema and transaction (SF-01, SF-03–SF-08)

Slice 4's single transactional migration depends on `sprint2_slice3_worker_attempt` and leaves prior migration bytes unchanged. Approved new tables (names are internal, not public APIs):

```sql
CREATE TABLE analysis_job_terminals (
    job_id UUID PRIMARY KEY REFERENCES analysis_jobs(job_id) ON DELETE RESTRICT,
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    terminal_kind TEXT NOT NULL CHECK (terminal_kind IN ('COMPLETED', 'FAILED')),
    finalized_at TIMESTAMPTZ NOT NULL,
    terminal_schema_version TEXT NOT NULL
        CHECK (terminal_schema_version = 'analysis_terminal_v1'),
    result_schema_version TEXT NULL,
    result_json JSONB NULL,
    model_version TEXT NULL CHECK (
        model_version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    processing_duration_ms BIGINT NULL CHECK (processing_duration_ms >= 0),
    safe_failure_code TEXT NULL
        CHECK (safe_failure_code ~ '^[a-z][a-z0-9_]{0,63}$'),
    failure_origin TEXT NULL CHECK (failure_origin IN
        ('CLASSIFIED_TERMINAL', 'CLASSIFIED_EXHAUSTION', 'EXPIRY_EXHAUSTION')),
    FOREIGN KEY (job_id, attempt_number)
        REFERENCES analysis_job_attempts(job_id, attempt_number) ON DELETE RESTRICT,
    CHECK (
        (terminal_kind = 'COMPLETED'
            AND result_schema_version IS NOT NULL
            AND result_schema_version = 'analysis_result_v1'
            AND result_json IS NOT NULL AND jsonb_typeof(result_json) = 'object'
            AND model_version IS NOT NULL AND processing_duration_ms IS NOT NULL
            AND safe_failure_code IS NULL AND failure_origin IS NULL)
        OR
        (terminal_kind = 'FAILED'
            AND result_schema_version IS NULL AND result_json IS NULL
            AND model_version IS NULL AND processing_duration_ms IS NULL
            AND safe_failure_code IS NOT NULL AND failure_origin IS NOT NULL)
    )
);

CREATE TABLE analysis_callback_outbox (
    callback_event_id UUID PRIMARY KEY DEFAULT gen_random_uuid()
        CHECK (callback_event_id <> '00000000-0000-0000-0000-000000000000'::uuid),
    job_id UUID NOT NULL UNIQUE
        REFERENCES analysis_job_terminals(job_id) ON DELETE RESTRICT,
    payload_schema_version TEXT NOT NULL
        CHECK (payload_schema_version = 'analysis_callback_snapshot_v1'),
    payload_json JSONB NOT NULL CHECK (jsonb_typeof(payload_json) = 'object'),
    delivery_state TEXT NOT NULL DEFAULT 'PENDING'
        CHECK (delivery_state IN ('PENDING', 'RETRYING', 'DELIVERED', 'EXHAUSTED')),
    created_at TIMESTAMPTZ NOT NULL,
    next_attempt_at TIMESTAMPTZ NOT NULL
);
```

The terminal PK gives at most one result/failure per job; the composite FK ties it to a real attempt; outbox unique job plus FK gives at most one event for that terminal. The repository transaction and read validation, not these checks alone, establish that the referenced attempt is current, properly resolved, and paired with the job's terminal state/time and outbox content. The SQL fixes a path-free bounded model-ID syntax, but OD-02 approves a configured logical ID for the exact child-reported player-model/ball-model pair; actual catalog values and deployment verification remain composition prerequisites, and no ID is fabricated here. Immutable content/association: every terminal column and outbox `callback_event_id`, `job_id`, `payload_schema_version`, `payload_json`, `created_at`. Delivery-mutable in a later slice: `delivery_state`, `next_attempt_at` (and only separately approved future delivery columns). Slice 4 inserts only `PENDING` with `next_attempt_at = created_at`; it neither claims nor sends. There is no event for `QUEUED`, `RUNNING`, requeue or `INTERRUPTED`, and no cancellation event rule here.

For a new success: lock J by supplied job ID, then its current A by unfiltered PK; decode and validate J/A, compare all four fence fields, require `RUNNING`/`ACTIVE`, and sample PostgreSQL T after locks. If the observed lease is expired, return `ClaimLost`. A guarded attempt UPDATE captures its own `clock_timestamp()` as `t_m`, matches complete fence/current count/status/job state and `lease_expires_at > t_m`, sets `SUCCEEDED`, `lease_expires_at = NULL`, `safe_failure_code = NULL`, `finished_at = t_m`, and returns exact A. Zero rows means `ClaimLost` without later writes. Validate preserved authority/start/heartbeat and `heartbeat <= finished_at < old_expiry`. Guarded job UPDATE changes only `RUNNING -> COMPLETED` and returns exact J. Insert R with `finalized_at = returned A.finished_at`, then insert O with `created_at = next_attempt_at = that same timestamp` and `DEFAULT gen_random_uuid() RETURNING` exact O/event ID. Validate each singleton, final J/A/R/O consistency, commit, close cursors, leave transaction and release connection before exposing `AnalysisJobCompleted`. No transaction spans CV analysis or callback transport.

The database generates the UUID **only on a genuinely new event INSERT**; replay, stale rejection and nonterminal paths make no generation call. PostgreSQL 16 provides built-in UUIDv4 [`gen_random_uuid()`](https://www.postgresql.org/docs/16/functions-uuid.html); the disposable harness pins PostgreSQL 16.15, while real deployment version remains an operations prerequisite. Read the authoritative UUID from `RETURNING`, validate exact built-in nonnil `UUID`, and return/reuse it. A UUID PK collision or any event-write/returned-evidence failure rolls back attempt, job and terminal writes; there is no silent internal retry or reuse of the lease factory. The same rule applies to failure events.

### Operation/outcome table

| Condition, after pre-I/O validation and required stored observations | Outcome and writes |
| --- | --- |
| Valid current unexpired `RUNNING`/`ACTIVE`, no R/O | New `SUCCEEDED` + `COMPLETED` + R + O in one commit; `AnalysisJobCompleted` with persisted finish/event ID. |
| Valid `COMPLETED`/current `SUCCEEDED`, exact complete fence, both R/O consistent, Q true for substantive projected result/full callback JSONB, and same schema/analysis/model/version metadata | `AnalysisJobCompletionAlreadyApplied` with **stored** finish/event ID; zero writes, no lease-expiry test. First-commit measurements remain unchanged. |
| Different substantive result/full callback JSONB (Q false), schema/analysis/model version, terminal kind, slot/lease/generation or finalizer kind after valid stored-state checks | `ClaimLost`; zero writes and no replacement. Only the two named measurements are replay-excluded; the job's stored analysis version is immutable, not upgraded. |
| Valid but expired active lease at post-lock T or guarded mutation time, stale authority or non-`RUNNING` state | `ClaimLost`; zero committed writes. |
| Missing required current A, or missing/duplicate/contradictory R/O for an observed terminal pair | Existing fixed invariant error; rollback, never successful replay. |
| Any pre-commit write, JSONB, FK, check, UUID collision or returned-evidence failure | Roll back **all** J/A/R/O changes; classify only established persistence failures; no callback. |
| Commit may have succeeded but response was lost | Caller retries the retained frozen snapshots and fence where available. If committed, replay under the exact C5 comparison returns original event/finish; if not and lease remains valid, a new commit may occur. Fresh changed substantive content is not promised acceptance. If lease was lost first, `ClaimLost`; do not infer commit from memory. |
| Existing terminal failure/at-limit exhaustion, same complete fence/finalizer and caller code `x` where applicable, expected terminal code/origin, valid R/O and Q true against the internally derived full failure callback JSONB | Existing `AttemptResolutionAlreadyApplied`; zero writes and stable stored event; no new event UUID or data rewrite. Different code/finalizer/derived callback or expiry-produced exhaustion is `ClaimLost`. |

## Failure terminalization and SF-08

The syntax `[a-z][a-z0-9_]{0,63}` is approved for `AttemptFailureCode`; the typed caller-supplied code `x` for classified terminal failure and `attempts_exhausted` for both exhaustion origins are approved by OD-03; syntax alone does not establish that an arbitrary `x` is safe for public disclosure. Slice 4 validates the exact type/syntax of the **caller-supplied** `AttemptFailureCode(x)` before I/O and uses it with the immutable job attempt limit; neither existing failure-finalizer signature gains a callback parameter or event factory. Child `ChildAnalysisFailure.error_code` remains an exception-class-style name and must not be used directly as a durable safe code. The 2026-10-17 owner amendment approves only typed `VIDEO_UNAVAILABLE` evidence from a positively missing file at child path resolution as terminal `AttemptFailureCode("video_unavailable")`; that code is safe for the existing failed-callback projection. Other real pipeline exception mappings, a production `attempt_limit`, and worker activation remain separate decisions. Unexpected errors, cancellation and fatal exceptions do not receive a guessed terminal code under this contract. The approved terminal mapping is:

| Source | Attempt/job | Terminal R code and origin | Current-shape failure callback snapshot |
| --- | --- | --- | --- |
| Caller-supplied terminal `AttemptFailureCode(x)` | `TERMINAL_FAILURE` / `FAILED` | `x`; `CLASSIFIED_TERMINAL` | `status: "failed"`, `error: {"code": x, "message": "Analysis could not be completed."}` |
| Caller-supplied retryable `AttemptFailureCode(x)` at immutable limit | `EXHAUSTED` with attempt code `x` / `FAILED` | `attempts_exhausted`; `CLASSIFIED_EXHAUSTION` | Same safe message, code `attempts_exhausted`; retain `x` only on the attempt, not public payload. |
| Lease expiry at immutable limit | `EXHAUSTED` with null attempt code / `FAILED` | `attempts_exhausted`; `EXPIRY_EXHAUSTION` | Same safe message and code. |
| Retryable below limit or expiry below limit | `RETRYABLE_FAILURE` or `INTERRUPTED` / `QUEUED` | No R, no O | No callback. |

The `attempts_exhausted` terminal/outbox code and fixed public message `Analysis could not be completed.` are approved by OD-03. Except for the narrowly approved `video_unavailable` terminal category, a concrete public `x` vocabulary still requires a safe classifier; valid syntax alone is insufficient. The production attempt limit remains a separate choice. These codes do not redefine callback delivery state `EXHAUSTED` or pipeline statuses. Every `FAILED` job above gets exactly one terminal event regardless of attempt count; an `EXHAUSTED` attempt does not get a second event. `CANCELLED` and its callback policy remain deferred.

**Failure/exhaustion consistency (OD-07).** There is no success `result_json` for these rows. The repository uses its required injected `FailureCallbackSnapshotV1Builder`, selected as the concrete pure `failure_callback_snapshot_v1` by composition, to freeze the expected `callback_json = failure_projection_v1(locked job_id, video_id, player_id, expected terminal safe code, approved fixed message)`; `request_id = str(job_id)`, `status = "failed"`, empty summary/ratings/events and the current no-`detailed` failure shape. Terminal failure uses validated caller-supplied `x`; retryable-at-limit retains caller-supplied `x` on A but derives terminal callback code `attempts_exhausted`; expiry-at-limit has no supplied code and also derives `attempts_exhausted`. The finalizer kind determines the expected R origin (`CLASSIFIED_TERMINAL`, `CLASSIFIED_EXHAUSTION` or `EXPIRY_EXHAUSTION`). After full fencing and J/A/R/O stored-state validation, including caller code where applicable, expected R code/origin, and exact O schema tag, replay Q compares this **derived** expected JSONB with persisted O; it never derives the expectation from O's payload. A contract assertion for each terminal kind makes the same stored-versus-deterministic-projection comparison. Existing Worker signatures remain unchanged: no callback argument and no event factory; exact replay returns the existing `AttemptResolutionAlreadyApplied` outcome while preserving the stored event UUID, without generating another UUID or rewriting data. The v1 mapper's output is bound to the immutable `analysis_callback_snapshot_v1` tag: a future mapping change must retain v1 behavior for v1 rows or obtain a separately approved version change, never silently reinterpret old events. The repository validates shape, association and invariants without recomputing exception classification. The failure mapper cannot manufacture a success result or callback before durable terminal state.

Evolve the existing Worker terminal finalizer, retryable-at-limit branch and recovery-at-limit branch **inside their existing transactions**: resolve A, change J to `FAILED`, insert R, insert O with PostgreSQL event UUID, validate complete returned set before commit. Preserve five signatures and existing `AttemptFailed`/`AttemptExhausted`/`RecoverySummary` public values. For exact terminal failure/exhaustion replay, read and validate R/O before returning the established already-applied outcome; below-limit requeue replay has no R/O. For recovery, the selected batch is currently handled inside **one** `_run` transaction. An R/O insertion or evidence failure rolls back the entire batch, including earlier candidate transitions and summary; no partial count escapes. The same malformed candidate can make later batches fail repeatedly until repaired, with backlog/alerting cost. This is a risk, not evidence of an observed indefinite poison pill; do not silently switch to per-job transactions or invent retry policy in Slice 4.

## Migration, privileges and enforcement limits (SF-07, SF-08)

Apply precondition: the deployment keeps durable workers/finalizers and incompatible Slice 3 terminal writers stopped; this operational writer-stop is **additional** to, not a substitute for, database locking. In one default transactional yoyo step, set migration `lock_timeout = '5s'` (OD-04). It limits each lock-acquisition wait, **not** total migration duration; a lock timeout aborts the entire migration without a partial revision and requires an operator-controlled later attempt, not automatic retry. Acquire `ACCESS EXCLUSIVE` on `analysis_jobs` **then** `analysis_job_attempts` before inspecting rows or DDL. This mode blocks concurrent readers/writers and is compatible with the `ALTER TABLE` needed for the status CHECK; accept its bounded lock-wait cost or abort without changes on timeout. Under the held locks, validate all existing J/A rows against Slice 3, preserve valid `QUEUED`/`RUNNING` and historical interrupted/retryable data, and refuse any pre-existing `COMPLETED`, `FAILED`, or `CANCELLED` job needing an absent/unsettled terminal representation. Do not invent a result or callback by backfill. Replace only the attempt status CHECK to admit `SUCCEEDED`, create R/O, then commit while locks remain held. Old incompatible writers must not restart after this migration; later compatible dark code still must not activate without the Slice 11 gate.

Downgrade first stops finalizers, then in one transaction locks jobs, attempts, terminal and outbox in that order with `ACCESS EXCLUSIVE` and the approved migration lock timeout, with the same per-lock acquisition-wait semantics rather than an overall deadline. Under locks, refuse if R or O contains any row, any attempt is `SUCCEEDED`, or any terminal job would be left with incompatible state; only then drop empty O/R and restore the exact Slice 3 attempt CHECK. Refusal rolls back the entire step and yoyo revision change, preserving both tables, all rows, immutable policy/count and schema signature. Do not drop or rewrite prior migrations. OD-04 approves the `5s` per-lock wait; operations still must approve the writer-stop rollout window/runbook before migration execution.

Provisioning owns roles/grants outside migration DDL because named runtime roles need not exist while migrations run. The migration owner owns tables; runtime and dispatcher roles must not. Finalizer runtime needs SELECT on J/A/R/O, the existing narrowly needed J/A UPDATE/INSERT rights and INSERT on R/O; it receives **no UPDATE or DELETE** on R, no UPDATE/DELETE of outbox identity/content/association, and no table ownership. A future dispatcher may receive SELECT of required event/job fields plus column-level UPDATE only on approved delivery columns (`delivery_state`, `next_attempt_at` and separately approved future delivery fields); no result/payload write privilege. The integration harness must provision comparable restricted roles and assert forbidden updates/deletes. PK/FK/CHECK/unique constraints, fenced repository writers and grants establish consistency under the supported writer model. A table owner, superuser or defective privileged writer can violate cross-table/state invariants without triggers; no trigger is added by default. A trigger-based stronger invariant is a separate owner choice with migration/write-cost implications.

## Candidate implementation scope and minimum evidence — separate authorization required

Future production paths: `src/adapters/migrations/postgres/sprint2_slice4_result_outbox.py`, `src/services/analysis_job_finalization.py`, `src/adapters/psycopg_analysis_job_finalization_repository.py`, and a pure conversion/presentation adapter for frozen result and callback snapshots. Existing-source edits: `src/domain/analysis_job_attempt.py` for `SUCCEEDED`; `src/adapters/psycopg_analysis_job_worker_repository.py` for C8/C9a, terminal failure/recovery and R/O observations; a narrow extraction of the current callback mapper from `src/api/routes.py` without changing route behavior. No runtime composition, sending, route selection, dispatcher, coordinator or process-count edit.

Future tests: `tests/test_analysis_job_finalization_contract.py`, `tests/test_postgres_result_outbox_migration_contract.py`, `tests/test_postgres_analysis_job_finalization_integration.py`. Evolve only affected `tests/test_analysis_job_worker_repository_contract.py`, `tests/test_postgres_analysis_job_worker_integration.py`, and migration-catalog/role-grant assertions in existing PostgreSQL integration tests. This document records the owner-approved contract. Any ADR or implementation-plan alignment requires separate authorization; neither is changed by this documentation increment.

- **Scripted contracts:** pre-I/O strict JSON/immutable bytes and same-input success/failure projection assertions; complete fence; C8/C9a replacement; success/terminal replay comparator including the exact two measurement exclusions; failure and exhaustion R/O creation; batch rollback; unchanged Worker signatures and lease-factory counts; cleanup precedence.
- **Source/static checks:** exact J/A/L/T preservation, per-operation R/O reads and separate Q layout; bound SQL/JSONB associations; job-before-attempt locks; guarded mutation clock; single event generation at insert; no transport; grants and prior-migration byte preservation.
- **Future disposable PostgreSQL evidence:** one coherent J/A/R/O set per terminal job; no R/O for requeue/interruption; stale/expired rejection; changed substantive snapshot/version/fence rejection; JSONB-equivalent formatting/numerics and both measurement-exclusion replays with first values intact; exact replay and stable ID after lost response; rollback after each necessary write and on event collision; FK/unique/CHECK and restricted-role immutability; upgrade writer lock, incompatible-row refusal, downgrade writer lock and refusal with schema/data preserved. Assert explicitly that missing R or O beside `COMPLETED`/`FAILED` is detected by required terminal paths and C9a, not by an unwritten global audit.

No tests, migration execution or implementation are authorized by this documentation decision. Even passing local PostgreSQL tests would not establish callback delivery, Apex compatibility, crash/cutover readiness, two-worker capacity or production resource safety.

## Approved owner decision register

| Decision | Approved selection | Remaining gate |
| --- | --- | --- |
| D1 / SF-05 / OD-06 wire | Existing body plus `callbackEventId` and `X-Super7-Callback-Event-Id`; no extra job-ID body/header fields or envelope. | Apex parsing, duplicate acknowledgement, authentication/replay and delivery authorization. |
| D2 / SF-06 / OD-05 replay | PostgreSQL JSONB Q equality for substantive content; exclude exactly terminal `processing_duration_ms` and top-level result `processing_time_ms` on both sides, retaining first values. | Comparator contract and live evidence. |
| D3 / SF-03 observation | Separate per-operation R/O reads/absence checks and inserted `RETURNING` validation; Q is one boolean row only on terminal replay. | Contract and live evidence. |
| D4 / SF-07 / OD-04 migration | Writer-stop plus ordered `ACCESS EXCLUSIVE` locks before preflight and DDL; `5s` per-lock acquisition wait with fail-fast rollback and operator-controlled later attempt, not a total migration deadline. | Operations rollout window/runbook and migration evidence. |
| D5 / SF-05 ID | PostgreSQL 16 `gen_random_uuid()` on new O insert; no factory. | Deployment PostgreSQL-version verification and collision/rollback evidence. |
| D6 / SF-08 / OD-03/07 codes | Terminal failure uses validated caller-supplied `AttemptFailureCode(x)`; both exhaustion causes use terminal/outbox code `attempts_exhausted`, with caller `x` retained on the classified attempt; fixed safe message and v1-derived replay event consistency. The 2026-10-17 amendment approves typed missing-video evidence as terminal `video_unavailable` only. | Other classifications and actual attempt limit later; Apex conformance before sending. |
| D7 / SF-08 scope | Atomic event in all Worker terminal branches; one recovery batch transaction. | Batch rollback evidence and operational visibility/repair. |
| D8 / SF-04 / OD-01/02/07 content/size | Same-input result/callback projection with typed retained metadata and a configured path-free logical ID for the exact model pair; no application byte ceiling. | Deployment catalog values/verification and projected-size measurement before activation. |
| D9 / SF-04/05 immutability | Constraints plus non-owner, least-privilege roles; no trigger by default. | Restricted-role and live integrity evidence. |

## Non-normative accepted risks and deferred gates

The owner accepts the no-cap storage/WAL/transport exposure of the explicit projection, not full legacy-response retention. The cited full-response samples are shape/size observations, not projected-size measurements or a maximum. Wrong or stale deployment model-pair catalog values can mislabel or block finalization; catalog values and verification are not supplied here. Syntactically valid caller code `x` is not automatically safe to disclose; the production classifier and attempt-limit configuration remain later coordinator/composition decisions. Apex payload parsing, duplicate acknowledgement, authentication/replay and non-scoring outcome conformance still gate sending. A failed terminal/outbox write in recovery rolls back the whole batch and may repeatedly block progress until visible repair; this is a cost, not evidence for a per-job redesign. A `5s` per-lock wait may abort under routine readers; the rollout window/runbook is still required. Owner/superuser or defective privileged writers remain outside runtime-grant protection.

**Evidence classification.** Approved contract: ADR-004/005/008 lifecycle/atomicity, Planning-F four-part fence and safe-code syntax, Planning-H C1–C10/C9a–b and cleanup, and this document's SF/OD decisions, with no score invention. Current implementation: inactive Slice 3 repositories/migrations and the current callback body/headers and child metadata shape. Future verification: the contract, migration and disposable-PostgreSQL evidence specified above; none was executed for this documentation increment. External/deferred: Apex conformance, deployment PostgreSQL/roles/storage/model catalog, worker composition, callback delivery, cutover and capacity evidence. No live Slice 4 behavior or production activation is claimed.
