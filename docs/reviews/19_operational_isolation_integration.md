# Phase 1.3 — Operational Isolation Integration

## Scope delivered

The existing `POST /analyze` route now keeps multipart form handling and temporary
upload staging on the async side, then routes the existing synchronous validation,
tracking, selection, analysis, debug rendering, and response construction body through
the existing `RequestLifecycle` and `AnalysisExecutor` worker boundary.

`create_app` now constructs one long-lived `AdmissionController`, `AnalysisExecutor`,
`ArtifactManager`, and `RequestLifecycle`. Each admitted analysis creates its own
`CancellationManager` and `ArtifactSession` through that lifecycle.

The debug source copy is staged and finalized through the request artifact session at
the same existing per-analysis debug location. The existing renderer and its returned
artifact paths are unchanged. Sessions retain the established debug directory behavior.

## Files created

- `docs/reviews/19_operational_isolation_integration.md`

## Files modified

- `src/main.py`
- `src/api/routes.py`
- `src/api/request_lifecycle.py`
- `src/concurrency/executor.py`
- `src/diagnostics/artifacts.py`

## Design decisions

- Operational singletons are composed once in `create_app`; they are explicitly passed
  to the router rather than discovered through globals.
- Request lifecycle admission occurs after multipart request validation and upload
  staging, before synchronous video/model/analysis work begins.
- `RequestLifecycle.execute_with_artifacts` creates request-scoped cancellation and
  artifact state, completes/cleans it in `finally`, and releases the admission permit.
- `AdmissionRejectedError` maps to the already-existing non-completed `failed` response
  shape; normal existing outcomes remain unchanged.
- `AnalysisExecutor` accepts the minimal cooperative cancellation behavior shared by
  the pre-existing state object and the manager, avoiding a duplicate per-request
  cancellation signal.
- Artifact retention defaults to unlimited when no retention count is provided, which
  preserves the prior debug-directory retention behavior while retaining an explicit
  configured retention mechanism for later deployment configuration work.

## Risks

- Current admission capacity is composed as one active analysis per process because no
  operational configuration field was introduced in this constrained phase. Capacity
  tuning requires a separate settings change and load characterization.
- Multipart upload persistence still precedes admission, so upstream body limits remain
  necessary for complete DoS control.
- The existing debug renderer writes its video/frame outputs into the owned session
  directory, but it is not yet individually staged/finalized by `ArtifactSession`.
- Cancellation is cooperative and is not yet connected to client-disconnect, deadline,
  or shutdown signals.
- The synchronous worker thread cannot force-stop active native OpenCV/model work.

## Tests added

No new integration fixture was added in this change. Existing deterministic API contract
tests exercise the newly wired route with injected fake trackers and preserve completed,
ambiguous, validation-error, and startup behavior. Existing lifecycle, executor,
admission, cancellation, and artifact unit suites cover their isolated ownership rules.

Remaining Phase 1.3 characterization gaps are explicit admission-exhaustion route,
disconnect/deadline cancellation, artifact-render cleanup, concurrent route capacity,
and composition-root identity tests using fakes without OpenCV. These require a later
dedicated route-lifecycle test seam to avoid altering public behavior.

## Verification results

```text
uv run pytest -q tests/test_analyze.py tests/test_health.py  # 5 passed
uv run ruff check .                                           # passed
uv run ruff format --check .                                  # passed
uv run mypy src tests                                         # passed (95 source files)
uv run pytest -q                                              # 124 passed
```

## Final status

Phase 1.3 wiring is complete for the existing synchronous analysis body. Routes,
response schemas, algorithms, thresholds, scoring, diagnostics fields, logs, and debug
paths remain compatible. The documented operational-test and cancellation-signal gaps
remain for follow-on hardening.
