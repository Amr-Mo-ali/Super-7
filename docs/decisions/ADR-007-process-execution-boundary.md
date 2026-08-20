# ADR-007: Process execution boundary

## Status

Accepted for the controlled MVP.

## Context

CPU-heavy analysis currently runs through `asyncio.to_thread`. ADR-006 selected process isolation but left its mechanism open. A custom spawn supervisor was prototyped in commits `1e8ad1a`, `af43dde`, and `f340410`; it required bespoke lifecycle, IPC, crash, and shutdown behavior before real analysis and repeatedly failed quality gates.

## Decision

The future boundary will use `ProcessPoolExecutor` with `multiprocessing.get_context("spawn")`, `max_workers=1` by default, the existing bounded `AnalysisQueue`, one pool submission at a time, a top-level child initializer, and a top-level pickle-safe callable. The API parent owns callback delivery and explicitly shuts down the executor during lifespan shutdown. Concurrency two remains forbidden pending VPS benchmark approval.

The parent owns FastAPI, validation/admission, queue and job state, executor submission, result validation, callback payload/delivery, observability, and shutdown. The child owns settings-snapshot validation, approved video resolution, model/tracker/pipeline construction, CPU-heavy analysis, artifacts/cleanup, and serialized success/failure output.

Only immutable pickle-safe request data crosses: analysis/video/player IDs, safe relative video reference, and explicit non-secret runtime configuration/version fields. Do not pass models, closures, loggers, callback URLs or services, FastAPI/asyncio objects, locks, lifecycle instances, open files, exceptions/tracebacks, environment dumps, or secrets.

## Limitations and rationale

Cancelling an awaiting future does not guarantee stopping running native inference. A hung function may require pool/container termination; per-task force termination is not normal `ProcessPoolExecutor` API behavior. `BrokenProcessPool` requires explicit handling or service degradation. Its internal queue is not admission storage: the bounded `AnalysisQueue` remains the boundary. Worker memory is duplicated and spawn/model initialization costs time. Callbacks and accepted jobs remain non-durable.

The custom supervisor was rejected because it added disproportionate concurrency infrastructure and failure modes without demonstrated benefit for one or two workers. Its prototype still clarified the spawn requirement, child-owned models, bounded waits, serialized boundaries, and shutdown risks.

Reconsider a custom supervisor or external task framework only for demonstrated per-job hard termination, worker-specific restart/health needs, multi-host workers, durable execution, higher throughput, or isolation unavailable from a pool.
