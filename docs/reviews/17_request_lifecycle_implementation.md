# Phase 1.1.D — Request Lifecycle Implementation

## Scope delivered

Implemented a minimal `RequestLifecycle` coordinator that combines the existing
`AdmissionController`, `AnalysisExecutor`, and `CancellationManager` around one
caller-supplied synchronous pipeline callable.

Its lifecycle is strictly:

```text
admit → create CancellationManager → execute supplied pipeline → complete cancellation state → release permit
```

It does not perform analysis, create artifacts/files, load models, manipulate HTTP
responses, or own services. It is intentionally not wired into FastAPI routes in this
phase, preserving every current endpoint, response structure, and route behavior.

## Files created

- `src/api/request_lifecycle.py`
- `src/concurrency/exceptions.py`
- `tests/api/test_request_lifecycle.py`
- `docs/reviews/17_request_lifecycle_implementation.md`

## Files modified

- `src/concurrency/executor.py`

The executor now accepts a narrow `CancellationSignal` protocol instead of only its
own concrete `CancellationState`. This has two real consumers: the existing executor
state and `CancellationManager`. The runtime behavior and existing executor interface
semantics are unchanged; the modification lets the lifecycle pass one request-scoped
cancellation object end-to-end without an adapter or duplicated cancellation state.

## Design decisions

- **Explicit constructor injection:** `RequestLifecycle` receives exactly an
  `AdmissionController` and `AnalysisExecutor`; it is not a service locator.
- **Immediate admission failure:** no permit produces `AdmissionRejectedError` before
  cancellation state creation or pipeline execution. The coordinator does not map this
  exception to HTTP.
- **One cancellation object per admitted request:** it is created after admission and
  passed directly into executor/pipeline work.
- **Original outcomes preserved:** the coordinator neither catches nor translates
  pipeline results or exceptions.
- **Deterministic cleanup:** `finally` always calls `CancellationManager.complete()`
  before releasing the permit. Permit release remains idempotent in its existing owner.
- **No route wiring:** route integration would change request lifecycle, capacity error,
  disconnect, and cancellation semantics and is intentionally deferred.

## Invariants

- A rejected request receives no permit, cancellation state, or pipeline execution.
- An admitted permit is released exactly once on return, exception, or cooperative
  cancellation.
- Pipeline exception type/message propagate unchanged.
- Cancellation remains cooperative; no thread/native interruption is attempted.
- Cancellation completion and permit release occur in `finally` after pipeline work.
- Request ID is passed unchanged to executor and cancellation snapshot.
- Each execution constructs independent cancellation state; concurrent requests cannot
  share it.
- No route, response schema, algorithm, threshold, score, tracker, or visualization
  code changed.

## Risks

- The coordinator is process-local and does not provide distributed capacity or durable
  jobs.
- It is intentionally unconnected to FastAPI; a later integration phase must
  characterize stable HTTP behavior for capacity rejection, client disconnect, timeout,
  and shutdown.
- `asyncio.to_thread` remains cooperative: a native call already running cannot be
  forcibly interrupted.
- The lifecycle has no artifact/resource ownership; existing pipeline owners remain
  responsible for their cleanup.

## Tests added

`tests/api/test_request_lifecycle.py` provides deterministic coverage for:

1. successful result/request-ID preservation;
2. admission failure with proof that the pipeline does not start;
3. unchanged pipeline exception propagation and permit release;
4. cooperative cancellation propagation and permit release;
5. completion/permit cleanup state after successful execution;
6. request-scoped cancellation isolation;
7. concurrent admitted execution;
8. direct-pipeline versus lifecycle result parity.

The exception, cancellation, and successful-cleanup tests jointly verify release on all
three outcomes. Tests use real small concurrency components and fake callables only; no
FastAPI, OpenCV, YOLO, GPU, network, or video input is used.

## Verification results

```text
uv run pytest -q tests/api/test_request_lifecycle.py  # 8 passed
uv run ruff check .                                   # passed
uv run ruff format --check .                          # passed
uv run mypy src tests                                 # passed (92 source files)
uv run pytest -q                                      # 113 passed
```

## Final status

Phase 1.1.D is complete as a small, standalone coordination layer. Existing routes
remain untouched, so all public API and analytical behavior is preserved while later
work has one explicit, tested integration seam.
