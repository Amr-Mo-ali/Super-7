> Status: Blocked decision register. Recommendations are proposed and are not Apex approval.

# Sprint 2 Apex decisions required

These decisions must be made without giving Super-7 access to Apex-owned tables. The existing
[proposed V1 contract](../../contracts/analysis-job-contract-v1.md) is input to review, not evidence
that Apex has implemented or approved the behavior.

| Decision | Recommended option | Alternatives requiring explicit selection | Consequence if unresolved | Owner | Blocks slice |
|---|---|---|---|---|---|
| `idempotencyKey` creator and reuse | Apex creates one opaque high-entropy key per logical analysis and reuses it for transport retries of that request. | Super-7-generated key supplied before admission; no idempotency for legacy requests. | Super-7 cannot distinguish a retry from intentional new work. Duplicate analysis remains possible. | Apex | 0, 2, 9, 11 |
| Caller/integration scope | Bind the key within an authenticated stable Apex integration scope. | One explicitly global key namespace; future tenant-specific scope. | A uniqueness constraint can collide across callers or fail to prevent cross-scope reuse. | Shared | 0, 2, 11 |
| Immutable request fingerprint | Include caller scope, `videoId`, `playerId`, normalized video reference, normalized callback destination, request schema version, and requested analysis version when supported. | Exclude only fields Apex explicitly permits to change; create a new key for changed delivery details. | Same-key mismatches may be silently merged or legitimate retries may conflict. | Shared | 0, 2, 11 |
| Intentional re-analysis | Use a new key and new `jobId`; automatic attempt recovery keeps the original key and job. | Explicit generation field under one business request; controlled redrive policy. | Super-7 cannot tell product-authorized re-analysis from a duplicate retry. | Apex | 0, 2, 9, 11 |
| `jobId` and `analysisId` compatibility | Make `jobId` canonical; during migration return `analysisId == jobId`. | Immediate `jobId` cutover; longer-lived alias. | Apex correlation and backward compatibility remain undefined. | Shared | 0, 2, 9, 11 |
| `callbackEventId` persistence and duplicates | Both services persist the stable event ID; Apex applies a duplicate as a successful no-op and returns an acknowledged 2xx. | Separate receiver idempotency key; no automatic redelivery until deduplication exists. | Lost acknowledgements can produce unsafe duplicate product updates. | Apex | 0, 4, 5, 11 |
| Callback authentication and replay | Approve a versioned signed or mutually authenticated callback contract with timestamp/replay controls and rotation ownership. | Approved gateway authentication; mTLS; defer callbacks from production activation. | Durable redelivery could amplify unauthenticated or replayable requests. | Shared | 0, 5, 11 |
| Callback retry and redrive window | Define bounded automatic retry and an operator redrive window; an approved redrive should retain the event identity unless the contract says otherwise. | Apex-triggered retry; no redrive; new event identity for explicitly new product events. | Attempt scheduling, retention, `EXHAUSTED`, and operator responsibility cannot be finalized. | Shared | 0, 5, 9, 11 |
| Result and idempotency retention | Retain result and idempotency/tombstone state for at least the maximum approved request-retry, callback-redrive, and replay window. | Different result and tombstone periods; longer compliance-driven retention. | Expired keys may recreate duplicate work, or results may disappear before recovery/redrive completes. | Shared | 0, 7, 9, 11 |
| Source-video availability | Apex keeps the referenced source available through terminal analysis or attempt exhaustion plus an approved recovery margin. | Super-7-owned immutable ingestion copy under a separately approved contract. | Recovered jobs can become impossible to execute even though their durable job record survives. | Apex | 0, 3, 10, 11 |
| Cancellation authority and callback semantics | Permit only authenticated Apex cancellation; define queued/running race behavior and whether terminal cancellation emits a callback. | No cancellation in the first durable contract; queued-only cancellation. | `CANCELLED`, recovery of cancellation-requested attempts, and terminal notification remain ambiguous. | Shared | 0, 6, 11 |
| Authoritative user-visible state | Apex owns user-visible product state; Super-7 owns execution, result, and delivery state and reports them through the approved contract. | A later Super-7 status API; shared read model. | Conflicting states can be presented as authoritative, especially when callback delivery is delayed. | Apex | 0, 5, 8, 9, 11 |

## Stop boundary

Recommendations in this register must not be copied into public request or callback behavior as if
they were approved. Slice 2 is persistence-only, and Slice 9 is compatibility/cleanup only; neither
has a public route activation boundary. Separately reviewed Slice 11 activation remains blocked
until every applicable row is approved, all applicable implementation slices are green, and Slice
10 crash-matrix evidence is approved.
