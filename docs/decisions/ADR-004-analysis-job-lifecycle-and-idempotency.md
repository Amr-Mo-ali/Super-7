# ADR-004: Analysis job lifecycle and idempotency

## Status

Accepted design for later implementation. It does not change the current runtime.

## 1. Current state (verified)

`POST /analyze` accepts JSON `videoId`, `playerId`, `videoUrl`, and `callbackUrl`; unknown fields are rejected. It creates a UUID4 `analysis_id` and returns HTTP 202 JSON with camel-case `analysisId`, `videoId`, `playerId`, and lowercase `status: "queued"`.

The active queue is a process-local bounded `asyncio.Queue`; its configured default capacity is 10. One lifespan-owned `AnalysisWorker` consumes FIFO jobs and stores job states (`QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`) in a process-memory dictionary. There is no durable job/result store, duplicate-request prevention, persisted callback-delivery state, or distinct callback status. Graceful queue shutdown cancels waiting jobs; a process crash/restart loses in-memory jobs, states, and results.

Callback delivery is attempted inline after the final analysis result (including a failed-analysis callback). `CallbackService` retries four total attempts with 1, 2, and 4 second delays for transport errors, timeouts, and non-2xx responses. A callback failure is logged but does not change a successful analysis from `COMPLETED`; it is not recoverable after process restart.

## 2. Decision: logical identities

| Identity | Creator / uniqueness / retry stability | Return, logging, later persistence |
|---|---|---|
| `jobId` | Super-7 creates an opaque globally unique identifier once for an accepted logical request. Stable for every request retry, analysis attempt, result, and callback. | Return on acceptance and duplicate lookup; log on every lifecycle event; persist as the primary job/result key. |
| `idempotencyKey` | Apex creates an opaque key once per logical request and reuses it only for retries of that request. It need not be globally meaningful outside its submitting Apex tenant/integration scope, but the Super-7 uniqueness constraint must include that scope if Super-7 serves more than one caller. | Return only when contract policy allows echoing it; log a safe fingerprint, not the raw key; persist with immutable request fingerprint and job ID. Never contain PII, video URLs, credentials, tokens, or secrets. |
| `videoId` | Apex-owned stable video identity. Not globally assumed by Super-7 without an agreed Apex scope. Immutable for one idempotency key. | Return, log, and persist with the job/result. |
| `playerId` | Apex-owned stable player identity. Immutable for one idempotency key. | Return, log, and persist with the job/result. |
| `callbackEventId` | Super-7 creates one opaque globally unique delivery-event identity after recording a terminal result. The same event ID is reused for every delivery retry and replay of that terminal event. | Send in callback header and payload, log, and persist in the callback-delivery record. |
| execution attempt | Super-7 creates an internal attempt identity whenever a worker claims the same job. It is not a new logical request or public job identity. | Persist with `jobId`, attempt count, `startedAt`/`finishedAt`, worker/lease identity where applicable, and safe error classification. |

`jobId` is the canonical Super-7 identity. `analysisId` is a temporary response/callback alias during migration and must equal `jobId`, never identify a second entity. Proposed HTTP acceptance/duplicate responses call the analysis state `status`; callback envelopes call the same analysis-status enum `analysisStatus`. This is a surface naming difference, not separate analysis state machines. Terminal HTTP metadata also exposes the independent callback-status enum.

One logical job has one `jobId`, one idempotency binding, one immutable request, and one final result or terminal failure. It may have multiple bounded internal execution attempts. Automatic attempts retain the same `jobId`, idempotency key, and resolved analysis version; they never create a duplicate logical job.

## 3. Idempotency semantics

One idempotency key identifies one immutable logical analysis request. The immutable fingerprint is: caller/integration scope, `videoId`, `playerId`, normalized video reference, normalized callback URL, `requestedAnalysisVersion` when supplied, and request schema version. Super-7 resolves and persists one concrete `resolvedAnalysisVersion` at acceptance. Neither version may be changed by a duplicate request.

| Situation | Required Super-7 behavior |
|---|---|
| New key | The durable acceptance boundary atomically records the immutable job, idempotency binding, resolved analysis version, and durable dispatch/enqueue intent. Return HTTP 202 with `duplicate: false` only after that succeeds. Worker dispatch may happen later. |
| Same key, same fields, `QUEUED` or `RUNNING` | Return the same job and current status with HTTP 202 and `duplicate: true`; do not enqueue another job. |
| Same key, same fields, terminal | Return existing terminal job metadata with HTTP 200 and `duplicate: true`, including analysis status, callback delivery status, and resolved analysis version. Do not rerun analysis or re-emit a callback. Do not promise a result or result summary before a result-query contract exists. |
| Same key, same fields, `CANCELLED` | Return the same job with HTTP 200 and `duplicate: true`; do not silently reactivate it. |
| Same key, different immutable field | Return HTTP 409 `idempotency_conflict`, include the existing `jobId`, and create/overwrite nothing. This includes changed `videoId`, `playerId`, video reference, callback URL, supplied `requestedAnalysisVersion`, or schema version. |
| Concurrent identical requests | Use an atomic unique key/fingerprint operation. Exactly one durable accepted job/dispatch intent is created; the other request receives the same job as a duplicate. |
| Apex times out before initial response | Apex retries with the same key. Super-7 returns the already-created job if acceptance occurred, or creates one job if it did not; it never creates two jobs. |
| Callback delivered more than once | Super-7 reuses the same `callbackEventId`. Apex must deduplicate by that ID and make repeated delivery a successful no-op. |

Worker crash, lost lease, process restart, and retryable infrastructure failure may return a recoverable `RUNNING` job to `QUEUED` for another bounded execution attempt. No callback is emitted for an intermediate attempt failure. `FAILED` is terminal only after a classified non-retryable analysis failure or exhaustion of later-configured `maxAttempts`. A deliberate product-level reanalysis after terminal `FAILED` requires a new key and job.

## 4. Proposed lifecycle

Analysis and callback delivery are independent state machines.

| Analysis state | Entry / allowed next states | Terminal / retry / required timestamps |
|---|---|---|
| `QUEUED` | Durable request/job and dispatch intent exist. Next: `RUNNING`, `CANCELLED`, `FAILED`. | Non-terminal. Dispatch failure leaves it durably queued and recoverable. Record `acceptedAt`, `queuedAt`. |
| `RUNNING` | A worker has durably claimed the job for one execution attempt. Next: `QUEUED`, `COMPLETED`, `FAILED`, `CANCELLED`. | Non-terminal. Record attempt ID/count, `startedAt`/`finishedAt`, worker/lease identity, and safe error class. A recoverable interruption returns to `QUEUED` while attempts remain. |
| `COMPLETED` | A finalized result was durably recorded. No analysis transition. | Terminal. Callback may still be pending/retrying. Record `completedAt`. |
| `FAILED` | A non-retryable failure or exhausted `maxAttempts` was durably finalized. No analysis transition. | Terminal. A failure callback is required when a callback target exists; deliberate reanalysis requires a new key/job. Record `failedAt`, safe failure code. |
| `CANCELLED` | Explicit cancellation, shutdown policy, or unrecoverable interrupted work is finalized. No analysis transition. | Terminal. Callback policy must be explicit before implementation; default is to notify when a callback target exists. Record `cancelledAt`, reason. |

| Callback state | Entry / allowed next states | Terminal / timestamps |
|---|---|---|
| `NOT_READY` | Job is not terminal or its terminal result is not durable. Next: `PENDING`. | Non-terminal. |
| `PENDING` | Terminal result and callback event are durable. Next: `DELIVERED`, `RETRYING`, `EXHAUSTED`. | Non-terminal. Record `callbackCreatedAt`, `nextAttemptAt`. |
| `RETRYING` | A transient delivery attempt failed. Next: `DELIVERED`, `RETRYING`, `EXHAUSTED`. | Non-terminal. Record attempt count and last safe error. |
| `DELIVERED` | Callback receiver acknowledged success. No delivery transition. | Terminal. Record `deliveredAt`, response class/code. |
| `EXHAUSTED` | Delivery policy ended without acknowledgement or permanent failure was classified. No automatic analysis transition. | Terminal. Record `exhaustedAt`, reason; operator/redrive policy may be required. |

A worker crash, service shutdown, or process restart must never infer `COMPLETED` from partial memory. Later implementation recovers durable state by reclaiming an expired/lost claim to `QUEUED` for another attempt when policy permits, or finalizing a visible terminal failure after attempts are exhausted. Callback status never changes analysis status: `COMPLETED` with `PENDING`, `RETRYING`, or `EXHAUSTED` is valid. A callback event is created only after the terminal result/failure is durable.

## 5. Failure taxonomy

| Failure | Before acceptance HTTP response | After acceptance state / retry / callback / operator |
|---|---|---|
| Request validation failure | 422; no job. | Not applicable. |
| Idempotency conflict | 409; no new job. | Existing job unchanged; visible to caller/operator. |
| Queue/admission rejection | 503 only before durable acceptance (closed admission, capacity rejection, or failed acceptance transaction); no job. | Not applicable. Dispatch failure after acceptance leaves the job `QUEUED`, never erases it. |
| Video unavailable | If detected before acceptance, 422; otherwise classify the attempt. | Retryable availability failures may requeue the same job; non-retryable or exhausted attempts become `FAILED` and require a failure callback. |
| Analysis failure | Not applicable. | Classify the attempt: retryable failures requeue within `maxAttempts`; non-retryable/exhausted failures become `FAILED` and require a callback. |
| Analysis timeout | Not applicable. | Classify as retryable or terminal by explicit policy; no callback until terminal state. |
| Worker crash/interruption | Not applicable. | Lost lease/restart may requeue the same job for another attempt; terminal failure only after non-retryable classification or exhaustion. |
| Result persistence failure | Do not send callback. | Do not mark `COMPLETED` or terminal `FAILED` until durable finalization succeeds; retain recoverable/visible state and require operator intervention if needed. |
| Callback transient failure | Not applicable. | Analysis terminal state unchanged; callback `RETRYING`, then `DELIVERED` or `EXHAUSTED`. |
| Callback permanent failure | Not applicable. | Analysis terminal state unchanged; callback `EXHAUSTED`; operator/redrive may be required. |
| Service shutdown | 503 once admission closes and before durable acceptance; no job. | Queued/running work follows durable cancellation/recovery policy; never discard accepted work merely because memory is lost. |

## 6. Invariants

- An accepted job is never represented only in process memory in the future implementation.
- Durable acceptance atomically records the immutable job, idempotency binding, and dispatch/enqueue intent before HTTP 202.
- A result is durably recorded before callback delivery is attempted.
- Callback failure never changes a completed analysis into a failed analysis.
- Duplicate HTTP requests never create duplicate logical jobs.
- Repeated callbacks reuse the same callback event identity.
- One job never overwrites another job’s result.
- A job cannot be `COMPLETED` without a finalized result.
- Missing callback delivery does not imply a missing analysis result.
- Idempotency conflicts are visible and never silently merged.

## 7. Compatibility and migration

Current clients send no `idempotencyKey`, `schemaVersion`, or requested analysis version, and receive only `analysisId`/lowercase `queued`. The proposed V1 contract is therefore a versioned API change, not a documentation-only relabeling.

Recommended rollout: first make Super-7 accept V1 fields and return both `jobId` and equal `analysisId`; Apex then stores `jobId`, generates/reuses opaque keys, and accepts versioned callback events idempotently. During a short migration window, legacy requests without `idempotencyKey` may use the current non-deduplicated behavior and must be explicitly labelled legacy; they cannot receive an idempotency guarantee. The final state requires `idempotencyKey` and `schemaVersion`, makes `jobId` canonical, and removes reliance on `analysisId` after Apex confirms migration.

`requestedAnalysisVersion` is optional and exists only if caller version selection is supported. `resolvedAnalysisVersion` is the concrete immutable version Super-7 selects and persists at acceptance; queued jobs retain it across deployments and every automatic attempt. `scoringVersion` is the concrete scoring implementation version in the result. `schemaVersion` versions the envelope. These fields are separate and must not be substituted for one another.

## 8. Deferred implementation

Later phases must implement a durable job store, atomic idempotency enforcement, durable queue/claiming, result persistence, callback outbox, startup recovery, lifecycle/delivery metrics, controlled worker concurrency, and load/crash testing. Selection of databases, queues, brokers, or orchestration technology is intentionally deferred.
