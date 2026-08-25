# Decisions, progress, and backlog

## Original state and completed work

Git history shows the original background queue/single worker was introduced in `5216705` (2026-08-08). Earlier audits describe synchronous CV via `asyncio.to_thread`; that is historical, not the current process-pool runtime. The current process boundary landed through commits headed by `1c5e464`/`28afdca` (2026-08-20). Logging refinements are at `85e6ee8` and current HEAD `75c1682`.

| Capability | Status | Evidence |
|---|---|---|
| Scoring, null/evidence, Overall, lifecycle/idempotency documentation | Documentation; current semantics partly implemented | ADR-001–005; source linked in scoring handoff |
| Bounded admission, one worker, lifespan lifecycle | Implementation complete and production-wired | [`analysis_queue.py`](../../src/services/analysis_queue.py), [`main.py`](../../src/main.py) |
| Spawned child, pickle-safe contracts, child-owned CV | Implementation complete and production-wired | [`process_analysis_pool.py`](../../src/services/process_analysis_pool.py), [`process_entrypoint.py`](../../src/services/process_entrypoint.py) |
| Parent callback/retries, state separation | Implementation complete and production-wired | [`routes.py`](../../src/api/routes.py), [`callback_service.py`](../../src/services/callback_service.py) |
| Artifact cleanup, health/readiness, structured lifecycle logging | Implementation complete and production-wired | `diagnostics`, `api/health.py`, tests |
| Smoke driver and benchmark runbook | Implemented documentation/test support; not a production claim | [runbook](../runbooks/process-pool-mvp-benchmark.md) |
| Production observation | Empirically observed only where stated in operations handoff | [operations](production-evidence-and-operations.md) |

## Decisions

| Decision / rationale | Trade-off and revisit trigger | Evidence |
|---|---|---|
| Move CPU work to spawned process boundary; avoid unsafe fork/model sharing | Startup/memory duplication; revisit for demonstrated hard-kill, worker-health or multi-host need | [ADR-007](../decisions/ADR-007-process-execution-boundary.md) |
| Exactly one active analysis / one child on 4 vCPU | Lower throughput; revisit only after benchmark | [ADR-006](../decisions/ADR-006-controlled-concurrency-mvp.md), source |
| Bounded in-memory queue rather than unlimited admission | Jobs disappear on restart; revisit when loss is unacceptable | [ADR-005](../decisions/ADR-005-durable-job-storage-and-worker-architecture.md) |
| Parent owns callbacks; callback failure never rewrites completed analysis | Retry blocks worker and is non-durable | ADR-004; processor tests |
| Null for unsupported evidence | Less complete-looking output; revisit only with new validated evidence | [ADR-003](../decisions/ADR-003-null-and-evidence-policy.md) |
| Do not use Apex DB as Super-7 queue | Requires Super-7-owned future persistence | ADR-005 |
| Exclude game intelligence from Overall | Overall remains potentially misleading with missing technical evidence | ADR-002 and engine |
| Do not increase YOLO/model size or process count without benchmark | Possible missed accuracy/throughput opportunity | Runbooks and observed CPU evidence |
| No new architecture without demonstrated failure mode | Defers sophistication | ADR-005/007 |

## Deferred work

| Deferred item | Why/risk now | Trigger and likely direction |
|---|---|---|
| Durable job/result storage, broker, atomic idempotency, callback outbox, restart recovery, multi-tenant scoping | Memory loss, duplicates, no recovery | Uncontrolled users/restarts/cost; Super-7-owned durable acceptance, result and delivery state without choosing technology yet |
| Callback auth/signatures and cancellation policy | Receiver trust and cancellation semantics incomplete | Before wider activation; define key/replay/rotation and explicit lifecycle policy |
| Hard child termination and multiple workers | Hung native work; CPU contention | Measured operational need; controlled supervisor/pool changes only after evidence |
| Target-player identity/re-identification | Scores may describe wrong person | Before credible player claims; identity signal/verification and track history |
| Validation set, event precision/recall, score calibration | No quality/ability claim defensible | Before claims; labelled datasets and held-out measurement |
| Shot outcome/difficulty, pass completion/value, teams, pitch/direction, tactical context, Overall redesign, intelligence validation | Present scores omit essential football context | Longer-term product evidence; add only with labelled/validated inputs |

## Safe next order

1. **Before credible MVP claims:** target identity, labelled event measurement, score-semantic review/Overall redesign, explicit UI limitations.
2. **Before increasing traffic:** callback authentication, operational monitoring, capacity/backpressure evidence and admission failure policy.
3. **Before parallel analysis:** cold/warm single-worker baseline, then controlled two-worker trial with CPU/RAM/latency/cleanup evidence.
4. **Before durable production:** Super-7-owned durable acceptance/idempotency/result/outbox/recovery and failure testing.
5. **Longer-term football intelligence:** teams, pitch calibration/direction, outcomes/value, tactical context and validated intelligence model.
