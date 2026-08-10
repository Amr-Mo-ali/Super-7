# Phase 7.2: Lifecycle Edge Fixes

## Objective

Correct two exceptional lifecycle paths without changing analysis behavior, tracker or
detector ownership, scoring, event arbitration, V2 schemas, admission capacity, or
execution topology.

`max_active_analyses` remains `1`. No queue exists and no model concurrency is enabled.
This phase fixes lifecycle correctness only.

## Exact pre-fix permit leak path

Before this change, `RequestLifecycle.execute_with_artifacts()` performed these steps
in order:

```text
admit permit
create CancellationManager
create ArtifactSession
enter try/finally for executor work
```

`ArtifactManager.create_session()` was outside the `try/finally`. If it raised, neither
`cancellation.complete()` nor `permit.release()` executed. `AdmissionPermit.release()`
is idempotent, but it could not run on that path.

## Exact pre-fix renderer handle leak path

Before this change, `render_debug_video()` created `cv2.VideoCapture`, optionally
created `cv2.VideoWriter`, executed the frame loop, then called `writer.release()` and
`capture.release()` after the loop. An exception during `read`, annotation, frame
writing, JPEG saving, or video writing bypassed those trailing release calls.

## Implemented ownership structure

### Permit, cancellation, and artifacts

Both lifecycle execution methods now acquire the permit, initialize optional
request-owned variables to `None`, and enter `try/finally` before creating the
`CancellationManager` or artifact session. Nested `finally` blocks provide this order:

```text
admitted permit
  → optional artifact cleanup, if created
  → optional cancellation completion, if created
  → permit release, exactly once
```

The nesting means artifact cleanup or cancellation completion failure cannot prevent
permit release. A missing cancellation manager is not completed, and a missing artifact
session is not cleaned. Session-creation exceptions still propagate unchanged.

### Renderer resources

The renderer now keeps its existing creation order—capture first, optional writer
second—and wraps rendering in `try/finally`. It attempts to release a created writer
once and the created capture once on success or failure. If rendering raised, that
exception remains primary even when a release raises. If rendering completed and a
release raises, the first cleanup error is raised after both release attempts.

Partial video/frame outputs retain their existing request artifact ownership. The
renderer does not delete or finalize artifacts; `ArtifactSession.cleanup()` remains
responsible for session cleanup.

## Files changed

| File | Change |
|---|---|
| `src/api/request_lifecycle.py` | Moved cancellation/session setup inside `try/finally`; guarded cleanup of resources that were created; made permit release unconditional after admission. |
| `src/services/debug_renderer.py` | Added exception-safe release of created capture and writer while preserving the primary rendering exception. |
| `tests/api/test_request_lifecycle.py` | Added deterministic artifact-session creation and cleanup-failure permit-release tests. |
| `tests/diagnostics/test_debug_renderer_lifecycle.py` | Added fake OpenCV success/failure lifecycle tests. |

## Invariants proven by tests

- Session-creation failure releases the permit and allows the next request to be
  admitted.
- Cancellation-manager initialization failure releases the permit without attempting
  cancellation completion.
- Artifact-cleanup failure still releases the permit.
- Existing lifecycle tests cover successful execution, worker failure, cancellation,
  idempotent permit release, nonnegative admission counts, and request isolation.
- Debug rendering releases capture after success, read failure, writer-creation failure,
  annotation failure, JPEG-save failure, and writer-write failure.
- A created writer is released after successful rendering and after write/annotation
  failure; no writer cleanup is attempted when writer creation raises.
- The original read/write/annotation/JPEG exception remains observable in the tests.

## Behavior preservation

The request pipeline, stage ordering, detector/tracker calls, settings, admission
limit, response mapping, V2 serialization, debug output names, and artifact ownership
are unchanged. The renderer returns the same debug-video and debug-frames mappings on
success. Cleanup only changes exceptional control flow.

## Tests

The tests use fake admission/artifact components and fake OpenCV handles; no models,
real video inference, GPU, or network access are used.

| Command | Result |
|---|---|
| `uv run pytest -q tests/api/test_request_lifecycle.py` | 11 passed |
| `uv run pytest -q tests/concurrency` | 22 passed |
| `uv run pytest -q tests/diagnostics` | 15 passed |
| `uv run ruff check .` | passed |
| `uv run ruff format --check .` | passed; 173 files already formatted |
| `uv run mypy src tests` | failed with 9 errors in `tests/test_public_contract_stability.py`, `tests/test_event_arbitration.py`, and `tests/test_phase_00_safety_hardening.py`; no error references a Phase 7.2 changed file |
| `uv run pytest -q` | 208 passed |

## Remaining concurrency risks

- Admission and artifact ownership are process-local.
- YOLO and third-party model-wrapper thread safety have not been characterized.
- The default `asyncio.to_thread()` executor is unchanged.
- This phase does not create cancellation during native OpenCV/model calls; it retains
  the existing cooperative stage-boundary model.

## Exact next phase

Characterize model-wrapper/thread safety and process-level resource ownership before
any change to `max_active_analyses`. Do not increase concurrency until that work is
complete.
