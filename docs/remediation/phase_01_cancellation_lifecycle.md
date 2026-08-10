# Phase 1: Cancellation & Lifecycle Hardening

## Objective

Make existing cooperative cancellation observable by the pipeline at lightweight stage boundaries, while retaining current permit, temporary-upload, and artifact-session ownership.

## Previous and new behavior

Previously `CancellationManager` recorded intent but `routes._analyze_uploaded` discarded it. Expensive later stages and optional rendering could continue. `CancellationChecker.check(stage)` now raises the dedicated `AnalysisCancelled` exception when a request has been cancelled. The route checks before/after major orchestration stages: validation, detection/tracking, selection, reconstruction, movement, interaction, event analysis, scoring, pass/shot detection, camera estimation, and rendering.

## Architecture and state transitions

`CancellationManager` owns request-local state. `CancellationChecker` is a read-only guard. The worker raises `AnalysisCancelled`; `RequestLifecycle.execute_with_artifacts` executes artifact cleanup, marks cancellation complete, and releases its admission permit in `finally`. The HTTP route logs cancellation separately and returns a non-stack-trace 499 response.

```text
CREATED -> ADMITTED -> RUNNING -> (FAILED | CANCELLED) -> CLEANUP -> COMPLETED
```

## Cleanup guarantees

Artifact session cleanup and permit release remain `finally` operations in `RequestLifecycle`; temporary uploads remain owned by `temporary_upload`. A checkpoint prevents later artifact rendering after cancellation. Cooperative checks cannot interrupt a detector or decoder mid-call; they stop the pipeline at the next completed stage boundary.

## Tests, risks, and lessons

Tests cover cancellation before execution, detection/tracking/scoring checkpoints, repeated cancellation, cleanup, permit release, artifact cleanup, and concurrent request isolation. The remaining risk is bounded cancellation latency within synchronous third-party calls. The lesson is that cancellation intent needs explicit consumers; lifecycle cleanup alone does not prevent wasted later work.
