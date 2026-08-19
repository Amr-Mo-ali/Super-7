# Analysis Job Contract V1 (proposed)

This is the proposed Super-7/Apex integration contract. It is not implemented by the current API, which accepts only four request fields and returns `analysisId` with lowercase `queued`.

## Recommendation

`jobId` is the canonical Super-7 identifier. Apex generates an opaque `idempotencyKey` and reuses it for retries of exactly one immutable request. `analysisId` remains a temporary alias equal to `jobId` during migration. A failed analysis requires a new key/job to retry. Idempotency conflict uses HTTP 409.

## Request

```json
{
  "videoId": "video-123",
  "playerId": "player-456",
  "videoUrl": "video-123.mp4",
  "callbackUrl": "https://apex.example.com/api/video-analysis/callback",
  "idempotencyKey": "opaque-random-key-generated-by-apex",
  "schemaVersion": "analysis_job_v1",
  "analysisVersion": "current"
}
```

`videoUrl` remains the existing safe relative shared-storage filename, not an external URL or backend filesystem path. `idempotencyKey` is opaque and must not embed PII, video/player IDs, URLs, credentials, access tokens, or secrets.

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
  "callbackStatus": "DELIVERED",
  "schemaVersion": "analysis_job_v1"
}
```

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
  "analysisVersion": "analysis-v1",
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
  "analysisVersion": "analysis-v1",
  "error": { "code": "video_unavailable", "message": "Analysis could not be completed." }
}
```

Required callback headers:

```text
Content-Type: application/json
X-Super7-Schema-Version: analysis_callback_v1
X-Super7-Callback-Event-Id: callback-901
X-Super7-Job-Id: job-789
X-Super7-Signature: <authentication/signature design to be finalized>
```

Apex must treat a repeated `X-Super7-Callback-Event-Id` as an acknowledged no-op and return a 2xx response.

## Fields

| Name | Type | Required | Owner | Immutable | Description |
|---|---|---:|---|---:|---|
| `videoId` | string | request/callback | Apex | yes | Apex video identity. |
| `playerId` | string | request/callback | Apex | yes | Apex player identity. |
| `videoUrl` | string | request | Apex | yes | Safe relative video reference. |
| `callbackUrl` | HTTPS URL | request | Apex | yes | Callback destination for this logical request. |
| `idempotencyKey` | opaque string | request | Apex | yes | Retry key; no PII or secrets. |
| `schemaVersion` | string | request/response/callback | shared | yes | Contract-envelope version. |
| `analysisVersion` | string | request/callback | shared/Super-7 | yes per job | Requested algorithm version; actual value is returned in callback. |
| `jobId` | opaque string | response/callback | Super-7 | yes | Canonical analysis job identity. |
| `analysisId` | opaque string | migration response/callback | Super-7 | yes | Temporary alias equal to `jobId`. |
| `callbackEventId` | opaque string | callback | Super-7 | yes | Stable event identity across delivery retries. |
| `analysisStatus` | enum | response/callback | Super-7 | mutable lifecycle | Analysis state, separate from delivery state. |
| `callbackStatus` | enum | duplicate/status response | Super-7 | mutable lifecycle | Delivery state; never implies analysis state. |
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

- The same key and immutable request fields return one job; different fields return 409.
- HTTP acknowledgement means accepted/known job, not completed analysis or delivered callback.
- A result is durable before its callback is attempted.
- Callback retry reuses one event ID; callback failure does not change `COMPLETED` to `FAILED`.
- Apex persists `jobId` and callback event IDs, and makes callbacks idempotent.
- Super-7 owns job/result/delivery state; Apex owns video retention and product-side updates.

## Explicitly deferred

Storage, queue, outbox, authentication/signature algorithm, retention windows, timeout values, worker count, callback redrive endpoint, cancellation endpoint, result-query endpoint, and the exact analysis/scoring version catalog are deferred. No implementation technology is selected by this contract.
