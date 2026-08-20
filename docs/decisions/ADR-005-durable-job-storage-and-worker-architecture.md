# ADR-005: Durable job storage and worker architecture

## Status

Accepted architecture decision for a later implementation phase. No runtime behavior changes in this ADR.

## Current operational architecture

Today one FastAPI process owns the API, an in-memory bounded `asyncio.Queue` (default capacity 10), its state dictionary, and exactly one lifespan-owned worker. It returns a UUID4 `analysisId` and HTTP 202, executes FIFO one job at a time, then attempts callbacks inline. Callback delivery has four attempts with 1/2/4-second delays, but delivery state is not persisted and a callback failure is only logged.

`docker-compose.yml` runs one `football-analysis` container, mounts models and the Apex-owned video volume read-only, and has no database or broker. The integration Compose file adds only an in-memory callback mock. Current dependencies include FastAPI, OpenCV, Ultralytics, and Pydantic; they include no database driver, ORM, migration tool, Redis client, or task framework. Restart/crash loses accepted job state/results; graceful shutdown cancels waiting work. Current CI tests the monolith and container build, not durable multi-process execution.

## Decision drivers

ADR-004 requires durable acceptance of the immutable job, idempotency binding, resolved analysis version, and dispatch intent before 202; durable results before callback; bounded attempts/leases; and recovery without duplicate logical jobs. The deployment is one Hostinger VPS (4 vCPU, about 15 GiB RAM), Docker Compose, CPU-heavy analysis, and an initial limit of two active analyses—one per worker. Super-7 must retain ownership of its own job/result state and must not write Apex application tables.

## Options considered

| Option | Summary | Advantages | Disadvantages and failure modes | Verdict |
|---|---|---|---|---|
| A. PostgreSQL-owned durable queue | Super-7 database tables hold jobs, attempts, results, and callback outbox; workers claim jobs from PostgreSQL. | One durable source of truth; transactional idempotency/acceptance/outbox; row-lock claims, leases, auditability, SQL inspection. | Requires database operations, migrations, polling/claim logic, and backup/monitoring. Database outage blocks durable acceptance/claims. | **Selected.** Smallest design that satisfies ADR-004 without cross-system dispatch gaps. |
| B. PostgreSQL + Redis + task framework | PostgreSQL owns records while Redis/framework dispatches tasks. | Mature worker tooling and broker features. | Adds broker service, worker framework, serializer/configuration, and database/broker dual-write or reconciliation gap; broker outage needs recovery anyway. Extra RAM/CPU/operations on one VPS. | Rejected now. Consider only after measured PostgreSQL claim/polling limits or required broker capabilities. |
| C. Redis-only | Redis holds queue/state/results. | Fast queue operations and simple worker libraries. | Less natural relational immutable idempotency/audit/result/outbox model; persistence configuration and data-loss risk must carry all correctness; weaker fit for transactional acceptance. | Rejected. Does not meet the durable relational contract as cleanly as PostgreSQL. |
| D. SQLite | One local database file backs queue/state. | Minimal installation and local development convenience. | Multi-process write/claim contention, migration/recovery/locking behavior, and host-volume durability are poor fit for two workers plus API; database outage and file locking are operationally awkward. | Rejected for production. May be a local test adapter only if separately proven. |
| E. Current `asyncio.Queue` | Keep process-local queue/state. | No new dependency or service. | Accepted jobs/results/callback state disappear on restart; no atomic idempotency or multi-process coordination. | Rejected. Contradicts approved contract. |

## Recommended architecture

Use **PostgreSQL-only as a Super-7-owned durable job queue, result store, and callback outbox**. PostgreSQL is the source of truth for queue depth, status, idempotency, attempts, results, and callback delivery; memory is a cache only.

- **API service:** validates input, resolves the concrete `resolvedAnalysisVersion`, and performs one durable acceptance transaction: immutable job, idempotency binding, initial `QUEUED` status, and durable dispatch intent. It returns 202 only after commit. Capacity/admission rejection or database transaction failure before commit returns 503 with no accepted job.
- **Analysis workers:** two separate worker processes, each with concurrency one. They claim one queued job in a short transaction using row locking with `SKIP LOCKED` (or equivalent), create an attempt, assign a lease, and commit before CPU-heavy analysis. They renew their own lease while running; they do not hold a database transaction while decoding or inferring.
- **Claim/dispatch strategy:** workers use bounded polling as the correctness mechanism, with optional PostgreSQL notifications only as a wake-up optimization. A missed notification cannot lose work because polling rechecks durable queued jobs.
- **Attempts/recovery:** a worker completes the same job by atomically recording attempt outcome and either requeueing a retryable attempt (within later-configured `maxAttempts`) or finalizing a terminal result/failure. Startup and periodic recovery reclaim expired/lost leases to `QUEUED` when eligible. Claim predicates and status/lease checks prevent concurrent valid claims.
- **Result finalization/outbox:** one transaction records the finalized result or terminal failure, terminal analysis status, and one callback-outbox event. No callback exists for intermediate attempt failures. The callback dispatcher marks delivery state separately and retries the same `callbackEventId`.
- **Callback dispatcher:** one lightweight process claims outbox records independently of CPU analysis workers. It supports durable retry/restart behavior and does not consume an analysis slot.

A separate broker is not necessary while PostgreSQL claims remain low-contention, two workers meet queue-wait objectives, and callback backlog is manageable. Reconsider only after measurement shows sustained claim/polling contention, unacceptable database load/latency, materially larger worker fleets or multiple hosts, or broker-specific delivery/throughput requirements that cannot be met with the durable database queue.

## Minimal Compose-level topology

```text
super7-api (1) ----+
super7-worker (2) -+--> super7-postgres (Super-7 database)
super7-callback (1)+
       |                         |
       +-- read-only /videos ----+-- durable jobs, attempts, results, outbox
```

The API and workers use the existing image with role-specific entry points in the later implementation. Each analysis worker initializes its own model pair and runs one job at a time; the two-worker target must be verified against RAM/CPU measurements before activation. Apex continues to own `/videos` and callback/product persistence. No Kubernetes, broker, or direct Apex database access is introduced.

## Ownership and security

PostgreSQL is logically owned by Super-7: separate database or schema, database user, migrations, backup/retention policy, and least-privilege credentials. Reusing an existing physical PostgreSQL server is an operational option only if it provides a separate Super-7 database/schema/user; Super-7 never reads or writes Apex-owned tables. Connection secrets are supplied through deployment runtime configuration, not committed files. Callback authentication/signing remains the separate pending contract decision from ADR-004.

## Operational risks and required recovery

| Risk | Required behavior |
|---|---|
| Database unavailable during acceptance | Return 503 before durable acceptance; create no job; Apex retries same key. |
| Database unavailable during execution | Worker must not lose in-memory analysis into a false completion; retry finalization until bounded policy/controlled shutdown, then recovery reconciles durable lease/attempt state. |
| Worker dies after claim | Lease expires; recovery records interruption and requeues the same job if attempts remain. |
| Worker dies after analysis before finalization | No terminal result/callback is assumed; expired lease recovery creates another attempt or finalizes only by explicit recovery policy. |
| Duplicate worker claim | Atomic claim predicate plus row locking permits one unexpired claim; stale claimant cannot finalize after lease ownership/version check fails. |
| Lease expires while work is running | Worker renews before expiry; expiry duration must exceed measured longest uninterruptible stage. If renewal is lost, worker stops/finalization is fenced and recovery handles the job. |
| API restart | Accepted jobs and dispatch intents remain queued; replacement API does not own execution recovery. |
| Database restart | API rejects new acceptance until healthy; workers recover/reclaim only after database readiness returns. |
| Callback worker restart | Outbox records remain pending/retrying; restart resumes due work with the same event ID. |
| Poison/repeatedly failing job | Bounded attempts; terminal `FAILED` with safe classification and one durable failure callback; operator visibility/redrive policy is required. |
| Disk growth/retention | Bound result/outbox/attempt retention and debug artifacts; monitor database volume and existing video/artifact disk separately; purge only under documented retention policy. |

## Scale boundary

Re-evaluate this architecture from measured queue depth and wait time, throughput, claim latency/lock contention, database CPU/IO, callback backlog/age, worker CPU/RAM saturation, and worker count. A broker or broader topology is justified only if these measurements show two workers and PostgreSQL claiming cannot meet service objectives, or if a multi-host worker requirement is established. No arbitrary global-scale threshold is assumed.

## Preferred answers

1. PostgreSQL-only is sufficient for the initial two-worker deployment: **yes**.
2. Redis required now: **no**.
3. Third-party task framework required now: **no**.
4. Poll/notify: **bounded polling, with optional notification wake-up later**.
5. Exactly-once-at-a-time claim: **short transactional row-lock/`SKIP LOCKED` claim plus lease fencing**.
6. Stale `RUNNING` recovery: **expire lease, record interrupted attempt, requeue same job if eligible**.
7. Long-analysis lease protection: **periodic renewal before a measured lease duration; fenced finalization**.
8. Finalization transaction: **terminal result/failure + job status + callback outbox record together**.
9. Callback delivery location: **one separate lightweight dispatcher process**.
10. Queue/status source of truth: **Super-7 PostgreSQL**.
11. Minimum constraints/indexes: **unique caller-scope/idempotency key; primary job ID; queued-claim index by status/availability; lease-expiry index; attempt-by-job index; callback-outbox due-status index; callback event-ID unique constraint**.
12. Broker trigger: **measured PostgreSQL/worker contention or multi-host/throughput need that cannot be met by this design**.
