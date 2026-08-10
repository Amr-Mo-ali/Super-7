# Phase 1.1.A — AdmissionController Implementation

## Scope delivered

Implemented one process-local `AdmissionController` and its deterministic unit tests.
It is intentionally not connected to FastAPI routes in this phase, so current endpoint
behavior, response contracts, algorithms, thresholds, and scoring are unchanged.

## Files created

- `src/concurrency/__init__.py`
- `src/concurrency/admission.py`
- `tests/concurrency/test_admission_controller.py`
- `docs/reviews/14_admission_controller_implementation.md`

## Files modified

None.

## Design decisions

- **Immediate admission, no queue:** `admit()` returns an `AdmissionPermit` when
  capacity exists and `None` when it does not. It never waits, stores pending work, or
  executes analysis.
- **Process-local scope:** capacity is local to the controller instance. This is the
  smallest implementation consistent with Phase 1.1.A and introduces no distributed
  infrastructure.
- **Explicit permit ownership:** an admitted caller owns one permit and may use it as
  an async context manager. Context exit releases the permit on normal return,
  exception, or task cancellation.
- **Exactly-once release effect:** repeated `release()` calls are idempotent; only the
  first decrements active capacity.
- **Atomic accounting:** a standard-library lock protects the four counters. The short
  counter operations contain no await point, so cancellation cannot interrupt a
  partially completed increment/decrement.
- **Typed metrics:** `metrics()` returns the immutable `AdmissionMetrics` snapshot:
  maximum capacity, active permits, admitted analyses, and rejected analyses.
- **No route integration yet:** integrating capacity rejection into HTTP status/error
  behavior requires a separately reviewed compatibility decision. Leaving it unwired
  preserves all existing public behavior for this focused component phase.

## Invariants enforced

- `active_permits` cannot exceed `max_active_analyses` because increment and capacity
  comparison occur under one lock.
- `active_permits` cannot become negative; an impossible controller release raises a
  clear internal error.
- A rejected admission returns no permit, so controller consumers have no resource that
  could begin an admitted analysis.
- Async-context exit releases permits after exceptions and cancellation.
- Manual repeated release is safe and does not leak or double-decrement capacity.

## Tests added

`tests/concurrency/test_admission_controller.py` covers:

- successful admission and metric snapshots;
- capacity exhaustion and rejected-work non-start;
- concurrent admission attempts without capacity overflow;
- manual and idempotent permit release;
- release after an exception;
- release after cancellation;
- invalid non-positive capacity.

All tests use only `asyncio` and standard-library synchronization. They do not use
YOLO, OpenCV, real videos, network calls, FastAPI, or production models.

## Risks and deliberate limits

- The controller is not an executor; callers must still use a permit context manager
  correctly when it is integrated in a later phase.
- Capacity is per process, not global across multiple Uvicorn workers or hosts.
- It deliberately provides immediate rejection rather than wait queues, priorities,
  timeouts, cancellation policy, artifact handling, or HTTP response translation.
- Model/tracker thread safety and blocking-work isolation remain outside Phase 1.1.A.

## Verification

Executed successfully:

```text
uv run pytest -q tests/concurrency/test_admission_controller.py  # 6 passed
uv run ruff check .                                               # passed
uv run ruff format --check .                                      # passed
uv run mypy src tests                                             # passed (85 source files)
uv run pytest -q                                                  # 89 passed
```
