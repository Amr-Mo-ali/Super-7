> Status: Super-7 contract direction accepted; Apex interoperability values and conformance evidence pending.

# Sprint 2 Apex decisions required

The project owner reports that Apex approved the Sprint 2 direction and the corrections requested
in the Super-7 review. Super-7 may proceed locally using the recommended contract direction and
deterministic fakes or test vectors. The repository still lacks itemized external values and Apex
conformance evidence. Missing Apex evidence does not block Slice 1 or unrelated local slices; it
blocks the first shared interoperability boundary that consumes it and production activation for
the relevant contract. These decisions do not give Super-7 access to Apex-owned tables. The
[proposed V1 contract](../../contracts/analysis-job-contract-v1.md) remains a versioned input to
implementation and interoperability review, not evidence that Apex has implemented the behavior.

| Decision | Recommended option | Alternatives requiring explicit selection | Consequence if unresolved | Owner | Local implementation dependency | Integration/cutover dependency |
|---|---|---|---|---|---|---|
| `idempotencyKey` creator and reuse | Apex creates one opaque high-entropy key per logical analysis and reuses it for transport retries of that request. | Super-7-generated key supplied before admission; no idempotency for legacy requests. | Super-7 cannot distinguish a retry from intentional new work. Duplicate analysis remains possible. | Apex | Slice 2 may implement the accepted recommendation with deterministic tests; none for Slice 1. | Verify Apex request behavior before route compatibility/cutover. |
| Caller/integration scope | Bind the key within an authenticated stable Apex integration scope. | One explicitly global key namespace; future tenant-specific scope. | A uniqueness constraint can collide across callers or fail to prevent cross-scope reuse. | Shared | Slice 2 may model the accepted caller-scoped contract locally; none for Slice 1. | Verify the real authenticated Apex scope before shared request integration and Slice 11. |
| Immutable request fingerprint | Include caller scope, `videoId`, `playerId`, normalized video reference, normalized callback destination, request schema version, and requested analysis version when supported. | Exclude only fields Apex explicitly permits to change; create a new key for changed delivery details. | Same-key mismatches may be silently merged or legitimate retries may conflict. | Shared | Slice 2 may implement this fingerprint against deterministic contract fixtures; none for Slice 1. | Verify Apex field normalization and version compatibility before route cutover. |
| Intentional re-analysis | Use a new key and new `jobId`; automatic attempt recovery keeps the original key and job. | Explicit generation field under one business request; controlled redrive policy. | Super-7 cannot tell product-authorized re-analysis from a duplicate retry. | Apex | Slice 2 may encode the accepted new-key/new-job rule locally; none for Slice 1. | Apex conformance is required before compatibility activation and Slice 11. |
| `jobId` and `analysisId` compatibility | Make `jobId` canonical; during migration return `analysisId == jobId`. | Immediate `jobId` cutover; longer-lived alias. | Apex correlation and backward compatibility remain undefined. | Shared | Slice 2 may persist canonical `jobId`; compatibility wiring remains Slice 9. None for Slice 1. | Verify Apex storage and alias handling before route cutover. |
| `callbackEventId` persistence and duplicates | Both services persist the stable event ID; Apex applies a duplicate as a successful no-op and returns an acknowledged 2xx. | Separate receiver idempotency key; no automatic redelivery until deduplication exists. | Lost acknowledgements can produce unsafe duplicate product updates. | Apex | Slices 4–5 may use deterministic duplicate-acknowledgement fixtures; none for Slice 1. | Apex duplicate/no-op conformance is required before callback integration and Slice 11. |
| Callback authentication and replay | Approve a versioned signed or mutually authenticated callback contract with timestamp/replay controls and rotation ownership. | Approved gateway authentication; mTLS; defer callbacks from production activation. | Durable redelivery could amplify unauthenticated or replayable requests. | Shared | The callback slice may define and test a reviewed local contract with deterministic vectors; no algorithm is selected here and Slice 1 is unaffected. | Matching Apex vectors, replay behavior, and rotation conformance are required before callback interoperability and cutover. |
| Callback retry and redrive window | Define bounded automatic retry and an operator redrive window; an approved redrive should retain the event identity unless the contract says otherwise. | Apex-triggered retry; no redrive; new event identity for explicitly new product events. | Attempt scheduling, retention, `EXHAUSTED`, and operator responsibility cannot be finalized. | Shared | Slice 5 requires a reviewed local retry/redrive policy; Slice 1 is unaffected. | Matching receiver and operator behavior is required before callback integration and Slice 11. |
| Result and idempotency retention | Retain result and idempotency/tombstone state for at least the maximum approved request-retry, callback-redrive, and replay window. | Different result and tombstone periods; longer compliance-driven retention. | Expired keys may recreate duplicate work, or results may disappear before recovery/redrive completes. | Shared | Later retention work needs reviewed local windows; Slice 1 is unaffected. | Apex retry/redrive windows and production retention approval are required before cutover. |
| Source-video availability | Apex keeps the referenced source available through terminal analysis or attempt exhaustion plus an approved recovery margin. | Super-7-owned immutable ingestion copy under a separately approved contract. | Recovered jobs can become impossible to execute even though their durable job record survives. | Apex | Slice 3 may use deterministic availability fixtures; Slice 1 is unaffected. | Verify Apex retention behavior before recovery activation, applicable Slice 10 crash tests, and Slice 11. |
| Cancellation authority and callback semantics | Permit only authenticated Apex cancellation; define queued/running race behavior and whether terminal cancellation emits a callback. | No cancellation in the first durable contract; queued-only cancellation. | `CANCELLED`, recovery of cancellation-requested attempts, and terminal notification remain ambiguous. | Shared | Slice 6 requires its own approved local race and callback contract; Slice 1 is unaffected. | Apex authority and receiver behavior are required before cancellation interoperability and cutover. |
| Authoritative user-visible state | Apex owns user-visible product state; Super-7 owns execution, result, and delivery state and reports them through the approved contract. | A later Super-7 status API; shared read model. | Conflicting states can be presented as authoritative, especially when callback delivery is delayed. | Apex | No Slice 1 dependency; Super-7 can implement its internal state locally. | Apex persistence/UI conformance is an integration and Slice 11 concern. |

## Cross-cutting evidence classification

| Area | Accepted local boundary | Pending external evidence | Effect |
|---|---|---|---|
| Callback HMAC | Super-7 may define and test the reviewed signed-callback contract with deterministic vectors in the applicable future slice. No algorithm is selected in this document. | Apex must supply matching canonicalization, encoding, headers, ordered fields, timestamp/skew, replay, key-ID/rotation, valid/invalid vectors, and corrected-configuration redrive behavior. | No Slice 1 dependency; required before callback interoperability and Slice 11. |
| Request JWT | Super-7 may define and test the reviewed local JWT contract in the first slice that consumes it. | Apex issuer, audience, algorithm, lifetime, skew, scopes, key distribution, rotation, revocation, and matching vectors remain pending. | No Slice 1 dependency; required before authenticated request interoperability and cutover. |
| OTP protection | No Super-7 durability dependency. | Apex owns its user-facing OTP controls and conformance evidence. | Outside Super-7 ownership; no local Slice 1 block. |
| Redis rate limiting | Redis is not required for Super-7 durability correctness. | Any Apex-owned rate-limiting policy and evidence remain Apex concerns. | Outside Super-7 ownership; no local Slice 1 block. |
| APX-15 result mapping | The [APX-15 result-mapping preparation](04-apx-15-result-mapping-preparation.md) is the accepted Super-7 canonical scoring and mapping direction. | Apex acknowledgement and proof that storage/API/UI preserve the versioned objects remain pending. | Local RED/GREEN work may proceed; conformance blocks shared integration and cutover. |

APX-15 acceptance covers canonical scoring names, states, attribution limitations, absence/null/zero
rules, and the Super-7/Apex ownership boundary. Apex must not flatten the canonical objects, remove
limitations, strengthen `NOT_VERIFIED`, or reinterpret evidence as ability. In particular, current
unsafe fields such as `speed_and_fitness`, `passing_and_playmaking`,
`shooting_and_finishing`, and `ball_control_and_individual_skill` must not be renamed into stronger
ability claims. Implementation, scientific validation, identity assurance, and Apex
interoperability remain pending.

## Integration and activation boundary

Accepted Super-7 directions may be implemented locally, but absent external values must not be
represented as received and interoperability must not be claimed before conformance evidence.
Slice 2 is persistence-only, and Slice 9 is compatibility/cleanup only; neither has a public route
activation boundary. Separately reviewed Slice 11 activation remains blocked until every
applicable external dependency is verified, all applicable implementation slices are green, and
Slice 10 crash-matrix evidence is approved.
