# Analysis Job Contract V1 (proposed)

This is the proposed Super-7/Apex integration contract. It is not implemented by the current API, which accepts only four request fields and returns `analysisId` with lowercase `queued`.

## Recommendation

`jobId` is the canonical Super-7 logical-job identifier. Apex generates an opaque `idempotencyKey` and reuses it for retries of exactly one immutable request. `analysisId` remains a temporary alias equal to `jobId` during migration. A job can have bounded internal execution attempts; automatic attempt retries retain its job/key. Only deliberate product-level reanalysis after terminal `FAILED` requires a new key/job. Idempotency conflict uses HTTP 409.

## Request

```json
{
  "videoId": "video-123",
  "playerId": "player-456",
  "videoUrl": "video-123.mp4",
  "callbackUrl": "https://apex.example.com/api/video-analysis/callback",
  "idempotencyKey": "opaque-random-key-generated-by-apex",
  "schemaVersion": "analysis_job_v1"
}
```

`videoUrl` remains the existing safe relative shared-storage filename, not an external URL or backend filesystem path. `idempotencyKey` is opaque and must not embed PII, video/player IDs, URLs, credentials, access tokens, or secrets. `requestedAnalysisVersion` is optional only when caller version selection is supported; if omitted, Super-7 selects and persists a concrete `resolvedAnalysisVersion` at acceptance.

## Acceptance and duplicate responses

New accepted job, HTTP 202:

```json
{
  "jobId": "job-789",
  "analysisId": "job-789",
  "videoId": "video-123",
  "playerId": "player-456",
  "status": "QUEUED",
  "duplicate": false,
  "resolvedAnalysisVersion": "analysis-v2026.08.19",
  "schemaVersion": "analysis_job_v1"
}
```

Duplicate while queued/running, HTTP 202:

```json
{
  "jobId": "job-789",
  "analysisId": "job-789",
  "videoId": "video-123",
  "playerId": "player-456",
  "status": "RUNNING",
  "duplicate": true,
  "resolvedAnalysisVersion": "analysis-v2026.08.19",
  "schemaVersion": "analysis_job_v1"
}
```

Duplicate terminal job, HTTP 200:

```json
{
  "jobId": "job-789",
  "analysisId": "job-789",
  "videoId": "video-123",
  "playerId": "player-456",
  "status": "COMPLETED",
  "duplicate": true,
  "resultAvailable": true,
  "schemaVersion": "analysis_job_v1"
}
```

Terminal duplicate responses contain job metadata only. They do not rerun analysis, re-emit a callback, or return a result/result summary. `resultAvailable` means a finalized result exists; it does not introduce a result-query endpoint.

Same key with different immutable fields, HTTP 409:

```json
{
  "error": "idempotency_conflict",
  "message": "idempotencyKey is already bound to a different immutable request.",
  "jobId": "job-789",
  "schemaVersion": "analysis_job_v1"
}
```

Validation error, HTTP 422:

```json
{
  "error": "validation_error",
  "message": "videoUrl must be a safe relative video filename.",
  "schemaVersion": "analysis_job_v1"
}
```

Queue/admission rejection, HTTP 503 (no job was accepted):

```json
{
  "error": "admission_rejected",
  "message": "Analysis capacity is temporarily unavailable.",
  "retryable": true,
  "schemaVersion": "analysis_job_v1"
}
```

## Callback envelope

Successful analysis callback:

```json
{
  "schemaVersion": "analysis_callback_v1",
  "callbackEventId": "callback-901",
  "jobId": "job-789",
  "analysisId": "job-789",
  "videoId": "video-123",
  "playerId": "player-456",
  "analysisStatus": "COMPLETED",
  "resolvedAnalysisVersion": "analysis-v2026.08.19",
  "scoringVersion": "player_rating_v1",
  "result": { "ratings": {}, "overall": null, "events": {} }
}
```

Failed-analysis callback:

```json
{
  "schemaVersion": "analysis_callback_v1",
  "callbackEventId": "callback-902",
  "jobId": "job-790",
  "analysisId": "job-790",
  "videoId": "video-123",
  "playerId": "player-456",
  "analysisStatus": "FAILED",
  "resolvedAnalysisVersion": "analysis-v2026.08.19",
  "error": { "code": "video_unavailable", "message": "Analysis could not be completed." }
}
```

Required callback headers in the proposed envelope:

```text
Content-Type: application/json
X-Super7-Schema-Version: analysis_callback_v1
X-Super7-Callback-Event-Id: callback-901
X-Super7-Job-Id: job-789
```

Apex must treat a repeated `X-Super7-Callback-Event-Id` as an acknowledged no-op and return a 2xx response.

`X-Super7-Signature` is reserved, not required or implementable yet. Signature/authentication must be finalized before production activation; this placeholder is not a security mechanism. A later decision must define the signature algorithm, secret/key ownership, timestamp, replay window, and rotation policy.

## Fields

| Name | Type | Required | Owner | Immutable | Description |
|---|---|---:|---|---:|---|
| `videoId` | string | request/callback | Apex | yes | Apex video identity. |
| `playerId` | string | request/callback | Apex | yes | Apex player identity. |
| `videoUrl` | string | request | Apex | yes | Safe relative video reference. |
| `callbackUrl` | HTTPS URL | request | Apex | yes | Callback destination for this logical request. |
| `idempotencyKey` | opaque string | request | Apex | yes | Retry key; no PII or secrets. |
| `schemaVersion` | string | request/response/callback | shared | yes | Contract-envelope version. |
| `requestedAnalysisVersion` | string | optional request | Apex | yes when supplied | Optional requested version; included in the immutable fingerprint only when version selection is supported. |
| `resolvedAnalysisVersion` | concrete string | response/callback | Super-7 | yes | Version selected and persisted at acceptance; unchanged across retries and deployments. |
| `jobId` | opaque string | response/callback | Super-7 | yes | Canonical analysis job identity. |
| `analysisId` | opaque string | migration response/callback | Super-7 | yes | Temporary alias equal to `jobId`. |
| `callbackEventId` | opaque string | callback | Super-7 | yes | Stable event identity across delivery retries. |
| `analysisStatus` | enum | response/callback | Super-7 | mutable lifecycle | Analysis state, separate from delivery state. |
| `resultAvailable` | boolean | terminal duplicate response | Super-7 | finalized | Whether a finalized result exists; it is not the result or a query promise. |
| execution attempt | internal record | no | Super-7 | immutable per attempt | Attempt identity/count, started/finished times, worker/lease identity, and safe error class. |
| `result` | object | successful callback | Super-7 | finalized | Versioned final analysis result. |
| `error` | object | failed callback/error response | Super-7 | finalized | Safe machine-readable failure information. |

## Statuses

| Analysis status | Meaning |
|---|---|
| `QUEUED` | Accepted and awaiting worker claim. |
| `RUNNING` | Worker owns execution. |
| `COMPLETED` | Final result is durable. |
| `FAILED` | Final failure is durable. |
| `CANCELLED` | Terminal cancellation is durable. |

| Callback status | Meaning |
|---|---|
| `NOT_READY` | No durable terminal result exists. |
| `PENDING` | Durable event awaits delivery. |
| `RETRYING` | A transient delivery failure occurred. |
| `DELIVERED` | Receiver acknowledged delivery. |
| `EXHAUSTED` | Delivery policy ended without acknowledgement. |

## Contract invariants

- The durable acceptance boundary records one immutable job, idempotency binding, resolved analysis version, and dispatch/enqueue intent before HTTP 202.
- The same key and immutable request fields return one job; different fields return 409.
- Worker crash, lost lease, restart, and retryable infrastructure failure may create a bounded new execution attempt for the same job; `RUNNING` returns to `QUEUED` and no intermediate callback is emitted.
- HTTP acknowledgement means accepted/known job, not completed analysis or delivered callback.
- A result is durable before its callback is attempted.
- Callback retry reuses one event ID; callback failure does not change `COMPLETED` to `FAILED`.
- Apex persists `jobId` and callback event IDs, and makes callbacks idempotent.
- Super-7 owns job/result/delivery state; Apex owns video retention and product-side updates.

## Explicitly deferred

Storage, queue, durable dispatch capability, authentication/signature algorithm, retention windows, timeout values, `maxAttempts`, worker count, callback redrive endpoint, cancellation endpoint, result-query endpoint, and the exact analysis/scoring version catalog are deferred. No implementation technology is selected by this contract.
