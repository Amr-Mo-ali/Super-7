# Phase 1.1.B — AnalysisExecutor Implementation

## Scope delivered

Implemented a minimal `AnalysisExecutor` that runs one supplied synchronous pipeline
callable in a worker thread via `asyncio.to_thread`. It preserves the callable's return
value and exception type, provides request-scoped context and cooperative cancellation
state, and owns no pipeline service, file, artifact, admission, configuration, or HTTP
behavior.

The component is intentionally not wired into `POST /analyze` in this phase. Route
integration would alter request lifecycle/cancellation behavior and requires separate
compatibility characterization. Existing API behavior therefore remains unchanged.

## Files created

- `src/concurrency/executor.py`
- `tests/concurrency/test_analysis_executor.py`
- `docs/reviews/15_analysis_executor_implementation.md`

## Files modified

None.

## Design decisions

- **One explicit execution method:** `execute(request_id, cancellation, pipeline)` is
  the entire public surface. The caller supplies an already-created synchronous
  pipeline callable and its request-scoped cancellation state.
- **Worker-thread isolation:** `asyncio.to_thread` keeps the supplied blocking callable
  off the event-loop thread without introducing a queue, executor pool policy, or
  distributed infrastructure.
- **No exception translation:** the executor returns the exact pipeline result or lets
  the original exception propagate unchanged.
- **Request context:** a `ContextVar` records the request ID only while the pipeline is
  running; it is reset in `finally` so concurrent executions cannot leak context.
- **Cooperative cancellation only:** `CancellationState` uses a thread-safe event that
  synchronous pipeline code may observe. Cancellation before invocation raises
  `asyncio.CancelledError`; in-flight native work must reach its own safe observation
  point. The executor never force-interrupts a worker thread.
- **No persistent executor state:** the executor has no mutable request state or
  long-lived service dependencies.

## Invariants

- Pipeline ordering is the responsibility of the supplied existing callable and is not
  changed by the executor.
- Supplied blocking work runs on a worker thread, not directly in the request coroutine.
- Each invocation receives its own request ID and `CancellationState`.
- The request context is always reset, including on exceptions and cancellation.
- Pipeline exceptions retain their original type and message.
- Pipeline-owned `finally` cleanup executes before its outcome returns from the worker.
- The executor does not transform results, load models/configuration, write files, or
  manipulate API responses.

## Tests

`tests/concurrency/test_analysis_executor.py` provides seven deterministic tests:

1. successful execution and worker-thread isolation;
2. original exception propagation;
3. pipeline cleanup after exception;
4. concurrent request-context/cancellation-state isolation;
5. cooperative cancellation observation and cleanup;
6. event-loop responsiveness while a worker pipeline blocks;
7. direct-versus-executor output parity.

They use only `asyncio`, `threading.Event`, standard-library context variables, and
fake callables. They do not instantiate FastAPI, YOLO, OpenCV, GPU work, network calls,
or real video inputs.

## Risks

- `asyncio.to_thread` does not terminate an already-running native model/OpenCV call.
  Later request-lifecycle work must define safe cancellation checkpoints and shutdown
  draining behavior.
- The current component does not set concurrency capacity; that remains the separately
  implemented, currently unwired `AdmissionController` phase.
- Per-process worker-thread behavior needs deployment-level capacity and model-thread
  safety validation before route integration.
- This component intentionally does not own resource cleanup; existing pipeline stages
  must continue to release their own resources, and future orchestration must preserve
  that ownership.

## Verification results

```text
uv run pytest -q tests/concurrency/test_analysis_executor.py  # 7 passed
uv run ruff check .                                            # passed
uv run ruff format --check .                                   # passed
uv run mypy src tests                                          # passed (87 source files)
uv run pytest -q                                               # 96 passed
```
