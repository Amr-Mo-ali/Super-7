# Super-7 canonical handoff

Snapshot: 2026-08-25. Inspected commit: [`75c1682a45a638a9c4140d4314506bb6b1183c30`](../../.git) (`feat(logging): implement scoped log capture for application logging boundary`). This handoff records the checked-out repository, plus production observations explicitly supplied for this handoff. It is not a production certification.

## Purpose and reading order

Super-7 is a Python/FastAPI modular monolith. It accepts a reference to an Apex-owned football video, runs computer-vision (CV) analysis asynchronously, and sends a callback with an evidence-gated, provisional rating projection. Apex owns product records and visual identity outside this service.

Read [scoring and product semantics](scoring-and-product-semantics.md), then [system and runtime](system-and-runtime.md), [decisions, progress, and backlog](decisions-progress-and-backlog.md), and [production evidence and operations](production-evidence-and-operations.md). Then consult [ADRs](../decisions/), the [proposed job contract](../contracts/analysis-job-contract-v1.md), and [runbooks](../runbooks/). Historical reviews/remediation notes are useful context, not a substitute for source.

Source precedence: 1. production code; 2. tests; 3. ADRs/contracts; 4. runbooks; 5. README; 6. future/backlog documents.

## Executive summary and maturity

Implemented and production-wired: one API process composes a bounded in-memory queue, one worker, and one spawned `ProcessPoolExecutor` child. The child owns CV work and artifacts; the parent validates the child result and owns callbacks. This isolates CPU-heavy work from the API event loop, but analyses are serialized and accepted jobs, results, and callback state do not survive restart.

The rating output is evidence-gated, but it is not calibrated football ability. It can return meaningful observational/video metrics and event confidence while still being misleading as an overall player assessment. Target selection is **not** player identity proof. Durability, idempotency, callback authentication, identity, validation data, event accuracy measurement, pitch/team context, and score calibration remain deferred.

## Known documentation mismatches

- The root [README](../../README.md) says the OpenAPI healthcheck does not add a health endpoint; current code implements `/health/live`, `/health/ready`, and `/health` in [`api/health.py`](../../src/api/health.py). The healthcheck itself still targets `/openapi.json`.
- The historical [repository audit](../repository_audit.md) and older reviews describe CV execution through `asyncio.to_thread`. Current composition uses [`ProcessAnalysisPool`](../../src/services/process_analysis_pool.py) and the spawned child entry point. Treat those statements as pre-2026-08-20 history.
- [ADR-004](../decisions/ADR-004-analysis-job-lifecycle-and-idempotency.md), [ADR-005](../decisions/ADR-005-durable-job-storage-and-worker-architecture.md), and the [V1 contract](../contracts/analysis-job-contract-v1.md) are accepted/proposed future designs, not current API behavior. Current requests have four fields, no idempotency key, and no durable state.
- The processor docstring in [`routes.py`](../../src/api/routes.py) calls the process processor unused, but [`main.py`](../../src/main.py) constructs it and passes it to the active worker. Code wiring is authoritative; the docstring is stale.

| Claim | Classification | Evidence/source | Confidence | Reverification trigger |
|---|---|---|---|---|
| One active analysis per process | Implemented and production-wired | [`analysis_queue.py`](../../src/services/analysis_queue.py), [`process_analysis_pool.py`](../../src/services/process_analysis_pool.py) | High | Any queue/pool configuration change |
| Queue default is 10 and is memory-only | Implemented and production-wired | [`core/config.py`](../../src/core/config.py), [`analysis_queue.py`](../../src/services/analysis_queue.py) | High | Storage/queue change |
| Callback failure does not turn success into failure | Implemented and production-wired | [`routes.py`](../../src/api/routes.py), [`test_process_analysis_job_processor.py`](../../tests/api/test_process_analysis_job_processor.py) | High | Callback/lifecycle change |
| Durable idempotent jobs | Documented decision | [ADR-004](../decisions/ADR-004-analysis-job-lifecycle-and-idempotency.md) | High that it is unimplemented | Any durable-store implementation |
| VPS timing/resource figures | Empirically observed | [operations evidence](production-evidence-and-operations.md) | Medium; supplied observations | New benchmark or deployment |
| Football validity/calibration | Unknown or requiring verification | No labelled validation dataset or calibration code found | High | Dataset, labels, and validation report |

## AI Agent Quick Start

Before changing code, answer:

1. Is this engineering correctness, football validity, product semantics, or infrastructure?
2. What source file, test, or ADR currently defines it?
3. Is it implemented, documented, or proposed?
4. What evidence gate protects the output?
5. Does it alter a public score or meaning?
6. Does it alter job lifecycle or callback behavior?
7. Does it alter concurrency or resource consumption?
8. Is a database/durable queue actually required for this MVP?
9. What focused tests prove it?
10. What safe production evidence would confirm it?
