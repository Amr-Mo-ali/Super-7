# ADR-010 — Opt-in local durable identity and callback dispatch

Status: owner-approved for the isolated local runtime only (2026-10-14).

The default `/analyze` composition remains the legacy four-field, in-memory
path. When the local durable composition is explicitly injected, the body stays
four fields (`videoId`, `playerId`, `videoUrl`, `callbackUrl`) and adds two
required headers: `Authorization: Bearer <configured secret>` and
`Idempotency-Key: <opaque Apex-generated key>`. A valid configured credential
maps server-side to the stable caller scope `apex`, including after credential
rotation. Missing or invalid credentials return 401; missing or invalid keys
return 422. A duplicate immutable request returns the original `analysisId`
(`jobId`) and a conflicting fingerprint returns 409. The raw key and bearer
credential are never persisted or logged. This is a deliberate header-based
compatibility choice, **not** the unchanged proposed V1 body contract. Apex
must confirm header support before API cutover.

The local dispatcher is one exclusive actor with at most one HTTP attempt in
flight. It selects only persisted PENDING/RETRYING events whose
`next_attempt_at` is due, waits one second when none is due, uses a ten-second
HTTP timeout, and persists RETRYING with a 30-second due delay after a failed
attempt. It never selects DELIVERED and reuses the immutable payload and event
ID. Delivery-state persistence errors are logged by safe type and delay
rediscovery by 30 seconds. There is no attempt ceiling, EXHAUSTED transition,
or redrive in this local increment; an unreachable receiver may be retried
indefinitely. Production exhaustion/redrive, callback authentication, and
multi-dispatcher coordination remain separate decisions. Callback delivery
cannot rewrite the terminal analysis result.

The coordinator originally had no approved durable child-failure classification.
An unclassified failure stops the worker incarnation and leaves recovery to
the lease boundary; this local runtime does not claim production fail-stop or
hard native-child termination behavior. Its lease, heartbeat, recovery batch,
idle cadence, attempt limit, model mapping, and secrets are explicitly
injected for an isolated experiment, not adopted as production defaults.

## Child-failure policy amendment (2026-10-17)

This supersedes the earlier deferred retryable `video_unavailable` policy.
When the requested video file is positively absent at child path resolution,
the child carries typed `VIDEO_UNAVAILABLE` evidence. After child work and
renewal settle, the coordinator calls the existing fenced terminal-failure
operation with `AttemptFailureCode("video_unavailable")`. The terminal
attempt/job/result/outbox and fixed safe failed-callback message remain one
atomic repository transaction. A confirmed resolution permits discovery of
another job; lost authority or ambiguous persistence stops this incarnation.
The composition root retains the required V1 result-projector and
failure-snapshot-builder injections; the coordinator supplies only the approved
safe code to the fenced repository operation.

This is a product policy, not a claim that the file can never appear later.
Apex must make the video available before submitting analysis. Intentional
analysis after supplying a previously missing file requires a new
`Idempotency-Key`; reuse of the old key returns its existing terminal job and
result. No retry scheduling, migration, setting, or worker sleep is added.

Missing storage roots, access errors, arbitrary path or I/O failures, decoder
errors, malformed envelopes, executor failures, and unknown child exceptions
receive no `video_unavailable` classification. Shutdown or cancellation alone
does not create a terminal failure or permit slot reuse while child work is
outstanding. Those unresolved cases retain stop-on-uncertainty and existing
lease/recovery behavior.

Normal durable HTTP admission rejects unsupported filename suffixes before
persistence. The owner-approved `UNSUPPORTED_VIDEO_FORMAT` terminal
mapping is therefore defensive for this composition and is not wired.

The opt-in Python composition requires validated `DatabaseSettings` (the
existing `POSTGRES_*` names), `Settings` for video/model paths, a bearer secret,
a distinct at-least-32-byte idempotency HMAC secret, attempt limit, coordinator
timing, recovery batch, worker idle cadence, concrete model catalog, callback
transport, and bounded repository operation/lock timeouts. None has a
committed credential value or a default durable-enablement environment flag.

The separate `durable_local_main:create_app` ASGI factory requires
`SUPER7_DURABLE_LOCAL_ENABLED=1`; ordinary `main:app` stays legacy. In
addition to the existing required `POSTGRES_*` settings and model/video
settings, the local factory requires:

- `SUPER7_APEX_BEARER_SECRET` and `SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX`;
- `SUPER7_DURABLE_ATTEMPT_LIMIT`, `SUPER7_DURABLE_MODEL_ID`,
  `SUPER7_DURABLE_RECOVERY_BATCH`, `SUPER7_DURABLE_WORKER_IDLE_SECONDS`;
- `SUPER7_DURABLE_LEASE_SECONDS`, `SUPER7_DURABLE_HEARTBEAT_SECONDS`,
  `SUPER7_DURABLE_WATCHDOG_SECONDS`, `SUPER7_DURABLE_JITTER_SECONDS`,
  `SUPER7_DURABLE_FINALIZE_SECONDS`,
  `SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS`,
  `SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS`;
- `SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS` and
  `SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS`.

The HMAC secret is an independent random secret of at least 32 decoded bytes.
No real `.env` file is part of this decision or increment.
