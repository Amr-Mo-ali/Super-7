# Final Production-Readiness Gate

## Classification: NOT_READY

This is **not** a model-quality judgment. The classification is based on unresolved
software-engineering and operational risks identified across all prior review reports.
It must not be upgraded to `PRODUCTION_READY` on the basis of the current manually
tested/synthetic-video coverage.

## Gate evidence

All reports under `docs/reviews/01_*.md` through `11_*.md` were reviewed.

| Required verification | Result | Evidence |
|---|---|---|
| No Critical findings remain | **Fail** | Phase 10 retains blocking synchronous request work and unbounded debug-artifact storage. |
| No unresolved High finding without accepted mitigation | **Fail** | Upload-body boundary, debug-path/data disclosure, request-ID/error consistency, Docker reproducibility/root runtime, and CI startup configuration remain unresolved. |
| Duplicate algorithms have justification | **Partial** | Pass/shot release and trajectory extraction are demonstrably duplicated. Phase 8B justifies a future bounded shared-evidence extraction but it has not been characterized/extracted. |
| No shared request-scoped mutable state | **Partial** | Per-analysis data is local, but the module-level app owns long-lived detector/tracker services. The lifecycle/concurrency budget remains unproven for concurrent production requests. |
| OpenCV resources are always cleaned up | **Fail** | Validator releases `VideoCapture`; debug rendering releases capture/writer only on normal completion, not via `finally`. Camera/debug exception paths require hardening. |
| API input limits exist | **Partial** | Application-level byte, duration, dimension, FPS, and suffix limits exist. Multipart parsing occurs before the application byte counter, so an upstream request-body limit is still required. |
| Errors are stable and safe | **Partial** | Unexpected errors use a generic 500 response, but expected errors and framework errors use inconsistent envelopes/statuses and errors omit an analysis/request ID. |
| Contracts are typed | **Pass, with limits** | Internal domain dataclasses and Pydantic API models are used. Some diagnostic payloads remain open dictionaries and rejected pass/shot candidates are not typed records. |
| Diagnostics reconcile | **Partial** | Completed-response invariants exist. Pass invalid-FPS diagnostics can report zero raw candidates and one rejection; rejected event evidence is generally aggregated rather than auditable record-by-record. |
| Tests are deterministic | **Mostly pass** | Current tests use fakes and tiny synthetic media; no normal test performs video model inference. OpenCV codec availability and default debug-output side effects remain environmental risks. |
| CI passes | **Fail** | Workflow is present, but its nonexistent `MODEL_PATH`/`BALL_MODEL_PATH` values conflict with constructor-time YOLO loading. Importing `main` in the health test therefore fails before tests run. |
| Production startup succeeds | **Conditional pass** | Import-only smoke test succeeded locally with the tracked `yolo11n.pt` file: `Football Analysis MVP 1.0.0`. It fails when configured weights are absent; startup is not independently deployable from model availability. |

## Commands run

| Command | Result |
|---|---|
| `uv run ruff check .` | Pass |
| `uv run ruff format --check .` | Pass |
| `uv run mypy src tests` | Pass — 82 source files |
| `uv run pytest -q` | Pass — 83 tests in 3.73 s |
| `uv run pytest --cov=src --cov-report=term-missing` | **Blocked** — `pytest-cov` is not installed; pytest rejects `--cov` arguments. |
| import-only startup smoke test | Pass with local tracked model weights; no video inference run |

The coverage command is an explicit final-gate failure: the repository has no
`pytest-cov` development dependency, coverage configuration, baseline, or CI coverage
step.

## Required closure before controlled pilot

1. Bound request concurrency and move blocking analysis/rendering off the event-loop
   request path, with explicit timeout/cancellation and resource-budget behavior.
2. Disable or strictly govern debug artifact generation in production; add retention,
   quota, access control, and non-path-leaking artifact references.
3. Enforce request-body limits before multipart parsing; define MIME/decoded-content
   policy and stable, request-correlated error responses.
4. Make every OpenCV writer/capture cleanup path exception-safe and test cleanup
   failures, including Windows file-handle behavior.
5. Fix CI so it either uses an explicitly supplied, integrity-checked local model
   artifact without download or prevents module-level model loading during tests.
   Then demonstrate a clean-checkout CI pass.
6. Add `pytest-cov`, lock it, configure reporting, and establish a reviewed coverage
   threshold. Run the required coverage command successfully in CI.
7. Make Docker consume `uv.lock`, run as non-root, define health checks and model
   delivery, and test the production image/start command.
8. Resolve or characterize shared pass/shot release and trajectory evidence, preserve
   rejected candidate records, and repair the invalid-FPS accounting invariant.

## Software-engineering readiness limitations

- The HTTP service has no admission control, deadline, cancellation, or proven
  concurrent model/GPU resource policy.
- Debug output is enabled in request processing and can retain raw uploaded media and
  one JPEG per frame without a lifecycle policy.
- Docker is not lockfile-based, runs as root, and lacks health checking.
- Coverage cannot currently be measured through the required command.
- CI's configured model paths contradict constructor-time model loading, so CI is not
  yet a verified clean-checkout gate.
- Configuration/environment validation and public-diagnostic exposure policy remain
  incomplete.

## Model-quality and analytical limitations

- The pipeline is validated mainly with deterministic synthetic cases and only a few
  manually exercised videos; it lacks a representative, labeled evaluation corpus and
  measured precision/recall by event type.
- Camera-motion diagnostics are not consistently applied to movement/pass/shot
  calculations, so image-space movement and trajectory evidence can be biased.
- Pass and shot candidates can overlap; receiver identity and tracking jumps are not
  fully explained by retained diagnostics.
- Technical scoring excludes pass and shot events and is provisional rather than a
  complete skill assessment.
- Physical scoring is image-space, provisional visible-movement activity; it is not a
  fitness or physical-performance assessment.

## Final decision

Do not deploy this build as a production service or classify it as ready for a
controlled pilot until the critical API/artifact risks, CI/coverage gate, and startup
model-lifecycle issue are closed and revalidated.
