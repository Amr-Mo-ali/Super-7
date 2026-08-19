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

`jobId` is the canonical Super-7 identity. `analysisId` is a temporary response/callback alias during migration and must equal `jobId`, never identify a second entity.

## 3. Idempotency semantics

One idempotency key identifies one immutable logical analysis request. The immutable fingerprint is: caller/integration scope, `videoId`, `playerId`, normalized video reference, normalized callback URL, requested analysis version, and request schema version. No field may be overwritten under an existing key.

| Situation | Required Super-7 behavior |
|---|---|
| New key | Atomically create the immutable request record and one `QUEUED` job, submit it, then return HTTP 202 with `duplicate: false`. If submission cannot be made atomically with acceptance, do not acknowledge success. |
| Same key, same fields, `QUEUED` or `RUNNING` | Return the same job and current status with HTTP 202 and `duplicate: true`; do not enqueue another job. |
| Same key, same fields, `COMPLETED` or `FAILED` | Return the same terminal job/result summary with HTTP 200 and `duplicate: true`; do not rerun analysis or emit another callback merely because of the HTTP retry. |
| Same key, same fields, `CANCELLED` | Return the same job with HTTP 200 and `duplicate: true`; do not silently reactivate it. |
| Same key, different immutable field | Return HTTP 409 `idempotency_conflict`, include the existing `jobId`, and create/overwrite nothing. This includes changed `videoId`, `playerId`, video reference, callback URL, analysis version, or schema version. |
| Concurrent identical requests | Use an atomic unique key/fingerprint operation. Exactly one job is created; the other request receives the same job as a duplicate. |
| Apex times out before initial response | Apex retries with the same key. Super-7 returns the already-created job if acceptance occurred, or creates one job if it did not; it never creates two jobs. |
| Callback delivered more than once | Super-7 reuses the same `callbackEventId`. Apex must deduplicate by that ID and make repeated delivery a successful no-op. |

A failed analysis is not retried by reusing its key. Apex creates a new idempotency key for a deliberate new analysis request, preserving the failed job as an auditable prior attempt.

## 4. Proposed lifecycle

Analysis and callback delivery are independent state machines.

| Analysis state | Entry / allowed next states | Terminal / retry / required timestamps |
|---|---|---|
| `QUEUED` | Durable request/job record exists and work is admitted. Next: `RUNNING`, `CANCELLED`, `FAILED`. | Non-terminal. No duplicate execution. Record `acceptedAt`, `queuedAt`. |
| `RUNNING` | A worker has durably claimed the job. Next: `COMPLETED`, `FAILED`, `CANCELLED`. | Non-terminal. Record `startedAt`, worker/attempt identity, heartbeat/lease as applicable. Crash/interruption must become recoverable work or a visible failure after recovery policy evaluation. |
| `COMPLETED` | A finalized result was durably recorded. No analysis transition. | Terminal. Callback may still be pending/retrying. Record `completedAt`. |
| `FAILED` | A finalized terminal failure was durably recorded. No analysis transition. | Terminal. A failure callback is required when a callback target exists; deliberate rerun requires a new key/job. Record `failedAt`, safe failure code. |
| `CANCELLED` | Explicit cancellation, shutdown policy, or unrecoverable interrupted work is finalized. No analysis transition. | Terminal. Callback policy must be explicit before implementation; default is to notify when a callback target exists. Record `cancelledAt`, reason. |

| Callback state | Entry / allowed next states | Terminal / timestamps |
|---|---|---|
| `NOT_READY` | Job is not terminal or its terminal result is not durable. Next: `PENDING`. | Non-terminal. |
| `PENDING` | Terminal result and callback event are durable. Next: `DELIVERED`, `RETRYING`, `EXHAUSTED`. | Non-terminal. Record `callbackCreatedAt`, `nextAttemptAt`. |
| `RETRYING` | A transient delivery attempt failed. Next: `DELIVERED`, `RETRYING`, `EXHAUSTED`. | Non-terminal. Record attempt count and last safe error. |
| `DELIVERED` | Callback receiver acknowledged success. No delivery transition. | Terminal. Record `deliveredAt`, response class/code. |
| `EXHAUSTED` | Delivery policy ended without acknowledgement or permanent failure was classified. No automatic analysis transition. | Terminal. Record `exhaustedAt`, reason; operator/redrive policy may be required. |

A worker crash, service shutdown, or process restart must never infer `COMPLETED` from partial memory. Later implementation must recover from durable state: reclaim a safely uncompleted lease, mark a terminal interruption according to policy, and leave already-finalized results intact. Callback status never changes analysis status.

## 5. Failure taxonomy

| Failure | Before acceptance HTTP response | After acceptance state / retry / callback / operator |
|---|---|---|
| Request validation failure | 422; no job. | Not applicable. |
| Idempotency conflict | 409; no new job. | Existing job unchanged; visible to caller/operator. |
| Queue/admission rejection | 503; no job/accepted key record. | Apex retries with the same key after backoff. |
| Video unavailable | If detected before acceptance, 422; otherwise `FAILED`. | Analysis retry requires a new key after video remediation; failure callback required. |
| Analysis failure | Not applicable. | `FAILED`; new-key retry only; failure callback required. |
| Analysis timeout | Not applicable. | Finalize `FAILED` or `CANCELLED` by explicit policy; no silent rerun; callback required. |
| Worker crash/interruption | Not applicable. | Recover/reclaim or finalize visible failure from durable state; operator intervention may be required; callback only after terminal state. |
| Result persistence failure | Do not send callback. | Do not mark `COMPLETED`; remain recoverable/visible as failure until a durable terminal record exists; operator may be required. |
| Callback transient failure | Not applicable. | Analysis terminal state unchanged; callback `RETRYING`, then `DELIVERED` or `EXHAUSTED`. |
| Callback permanent failure | Not applicable. | Analysis terminal state unchanged; callback `EXHAUSTED`; operator/redrive may be required. |
| Service shutdown | 503 once admission closes; no job. | Queued/running work follows durable cancellation/recovery policy; never discard accepted work merely because memory is lost. |

## 6. Invariants

- An accepted job is never represented only in process memory in the future implementation.
- A result is durably recorded before callback delivery is attempted.
- Callback failure never changes a completed analysis into a failed analysis.
- Duplicate HTTP requests never create duplicate logical jobs.
- Repeated callbacks reuse the same callback event identity.
- One job never overwrites another job’s result.
- A job cannot be `COMPLETED` without a finalized result.
- Missing callback delivery does not imply a missing analysis result.
- Idempotency conflicts are visible and never silently merged.

## 7. Compatibility and migration

Current clients send no `idempotencyKey` or `schemaVersion`, and receive only `analysisId`/lowercase `queued`. The proposed V1 contract is therefore a versioned API change, not a documentation-only relabeling.

Recommended rollout: first make Super-7 accept V1 fields and return both `jobId` and equal `analysisId`; Apex then stores `jobId`, generates/reuses opaque keys, and accepts versioned callback events idempotently. During a short migration window, legacy requests without `idempotencyKey` may use the current non-deduplicated behavior and must be explicitly labelled legacy; they cannot receive an idempotency guarantee. The final state requires `idempotencyKey` and `schemaVersion`, makes `jobId` canonical, and removes reliance on `analysisId` after Apex confirms migration.

Request, acceptance-response, callback-envelope, analysis-algorithm, and scoring-algorithm versions are separate fields. Algorithm/scoring versions describe the generated result; they must not be substituted for request schema version.

## 8. Deferred implementation

Later phases must implement a durable job store, atomic idempotency enforcement, durable queue/claiming, result persistence, callback outbox, startup recovery, lifecycle/delivery metrics, controlled worker concurrency, and load/crash testing. Selection of databases, queues, brokers, or orchestration technology is intentionally deferred.
