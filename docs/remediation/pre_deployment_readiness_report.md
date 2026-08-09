# Pre-deployment readiness report

Audit date: 2026-08-08  
Scope: repository inspection only. No application code, configuration, or deployment files were changed by this audit.

## 1. Executive summary

**Decision: NO GO.**

The repository has a passing runtime test suite (210 passed) and clean Ruff results, but it is not ready for a production-server deployment. The supplied Docker image cannot contain the default model artifact because its `Dockerfile` copies only `pyproject.toml`, `README.md`, and `src/`, while `Settings` defaults both model paths to the repository-root `yolo11n.pt`. The repository also provides no health/readiness endpoint, no request deadline implementation, no application shutdown handling, no Compose definition, and a failing mandatory type-check command.

The API contract is deliberately constrained and tested, but its operational behavior is not sufficient for production deployment: uploads are persisted before analysis admission, admission rejection is represented as a Public Rating V2 body with HTTP 200, and no external or application deadline bounds request execution.

## 2. Repository status

| Item | Observed status | Evidence |
| --- | --- | --- |
| Public route count | One route: `POST /analyze` | `src/api/routes.py:97-104`; `src/main.py:70-84` |
| Contract | Response model is `PublicRatingV2Response | PublicRatingV2Failure` | `src/api/routes.py:97-104` |
| Application version | Defaults to `1.0.0` | `src/core/config.py:20-31` |
| Default model | Repository-root `yolo11n.pt` exists (5,613,764 bytes); both player and ball default paths are `yolo11n.pt` | root `yolo11n.pt`; `src/core/config.py:58-70, 204-212` |
| Docker copy set | Only `pyproject.toml`, `README.md`, and `src/` are copied | `Dockerfile:1-12` |
| Compose file | None found by repository file search | `rg --files -g '*compose*' -g 'compose.*'` |
| CI | Runs lint, format, mypy, and pytest; it does not build/run the Docker image | `.github/workflows/ci.yml` |

## 3. API stability

### Contract and validation

`POST /analyze` accepts a single multipart `video` field and uses an explicit response model. It rejects additional multipart fields and permits only `.avi`, `.mkv`, `.mov`, and `.mp4` filename suffixes. The uploaded content is persisted in 1 MiB chunks and checked against `MAX_UPLOAD_BYTES`; OpenCV then checks that it opens, has a decodable frame, valid FPS/frame count, allowed duration, and minimum dimensions.

Evidence: `src/api/routes.py:97-113`; `src/services/video_validator.py:17-97`.

The response model is present, so successful and modeled non-completed results are response-validated by FastAPI. Error responses raised as `HTTPException` do not use a declared error response model.

### Error and status behavior

| Condition | HTTP behavior implemented | Evidence |
| --- | --- | --- |
| Validation / upload / video error (`AnalysisError`) | 422 with `{"error": ...}` | `src/api/routes.py:154-158` |
| Cooperative cancellation | 499 with `{"error": "Analysis cancelled."}` | `src/api/routes.py:151-153` |
| Unexpected exception | 500 with fixed generic body | `src/api/routes.py:159-161` |
| Capacity exhausted | HTTP 200 Public Rating V2 failure body | `src/api/routes.py:139-145` |
| Detector not configured | HTTP 200 Public Rating V2 failure body | `src/api/routes.py:146-150` |

The repository does not provide a route that invokes `CancellationManager.request_cancellation`, `expire_deadline`, or `request_shutdown`; those methods occur only in the manager and tests. There is no application lifespan/shutdown handler. The documented cancellation behavior is therefore cooperative infrastructure that is not connected to client disconnects, deadlines, or application shutdown in the inspected routing/application code.

Evidence: `src/concurrency/cancellation.py:40-94`; search of `src/` and `tests/` for those method calls; `src/main.py`.

### API conclusion

The public request/response shape is constrained and covered by tests, but the API is **not production-ready** because the operational error semantics, deadline handling, health behavior, and pre-admission upload behavior do not provide a deployable service boundary.

## 4. Test results

The requested command was run on 2026-08-08:

```text
uv run pytest -q
210 passed in 4.04s
```

Result: 210 passed, 0 failed, 0 skipped.

The suite includes API, lifecycle, cancellation, concurrency, diagnostics, public-contract, and tracker-isolation test modules. This audit found no failed test run. Passing tests do not exercise a container build/run: the CI workflow has no Docker build job.

## 5. Static analysis results

| Command | Result |
| --- | --- |
| `uv run ruff check .` | Passed: `All checks passed!` |
| `uv run ruff format --check .` | Passed: `179 files already formatted` |
| `uv run mypy src tests` | Failed: 9 errors in 3 test files, 138 files checked |

Mypy output:

```text
tests/test_public_contract_stability.py:124: union-attr (reason_code)
tests/test_event_arbitration.py:13: arg-type (event type literal)
tests/test_event_arbitration.py:27: 6 arg-type errors (dataclasses.replace kwargs)
tests/test_phase_00_safety_hardening.py:11: attr-defined (DebugSettings export)
```

The repository has no baseline report that attributes these errors as pre-existing or newly introduced. The audit can establish only that they exist in the inspected revision. Since `.github/workflows/ci.yml` runs this same mypy command, the current CI quality job fails at its type-check step.

## 6. Logging review

Logger ownership is standard-library named logger retrieval only: `get_logger(name)` returns `logging.getLogger(name)` and supplies no formatter, handler, level, JSON/structured format, or request context configuration (`src/core/logging.py`).

The application creates named startup, API, player-detector, and ball-detector loggers in `src/main.py:45-84`. Exception logging exists in the API for unexpected failures and selected pipeline-stage failures (`src/api/routes.py:159-161, 413-414, 589-590, 626-627, 671-672, 1062-1063`). Some stage logs include `analysis_id`; the top-level unexpected-error log (`analysis_failed`) and validation warning do not.

Both YOLO adapters emit one `INFO` record for every frame inference (`src/adapters/yolo_player_detector.py:66-74`; `src/adapters/yolo_ball_detector.py:75-83`). No structured logging setup, request ID logging filter, admission metrics emission, cleanup-result logging, or process-resource logging was found.

Conclusion: logging is insufficient for production observability. The repository provides named logs and selected exception stack traces, but no configured production logging policy or complete request correlation.

## 7. Docker review

| Topic | Observed implementation | Readiness result |
| --- | --- | --- |
| Base image | `python:3.12-slim` | Present |
| Startup | `uvicorn main:app --host 0.0.0.0 --port 8000` | Present |
| Exposed port metadata | No `EXPOSE` instruction | Missing |
| Health check | No `HEALTHCHECK` instruction | Missing |
| User | No `USER` instruction; container runs with image default user | Missing non-root configuration |
| Models | Model artifact is not copied into image; no model mount/download setup is defined | **Blocking** |
| Workers / timeouts / graceful shutdown | No Uvicorn worker, timeout, or shutdown arguments | Not configured in image |
| Image size | No built-image size artifact or CI image build exists | Cannot be determined from repository evidence |
| Environment variables | No Docker `ENV` values beyond `PYTHONPATH`; no runtime deployment manifest | Incomplete |

The image will contain no repository-root `yolo11n.pt`. The default `MODEL_PATH` and `BALL_MODEL_PATH` resolve to that filename. `YOLOPlayerDetector` and `YOLOBallDetector` instantiate Ultralytics `YOLO` using those paths during app construction (`src/main.py:45-49`; `src/adapters/yolo_player_detector.py:24-39`; `src/adapters/yolo_ball_detector.py:19-38`). The repository does not define an alternate provision mechanism. It therefore does not provide an image that can be verified to start with its default model configuration.

## 8. Docker Compose review

No `docker-compose*` or `compose.*` file exists. README states that Compose is not needed and only documents `docker build -t football-analysis .` (`README.md`). There is no repository deployment definition for ports, model volume, environment file, health checks, restart policy, persistent storage, resource constraints, or reproducible local service start. Local deployment is not reproducible from a Compose definition because none exists.

## 9. Environment review

`.env.example` documents seven values: `MAX_UPLOAD_BYTES`, `MAX_DURATION_SECONDS`, and five debug-artifact settings. `Settings.from_environment()` additionally reads player-model path/device/confidence/IoU/image size, ball-model path/confidence/IoU/image size, selection mode, target-segment settings, tracklet stitching, segment-ball settings, and debug settings (`src/core/config.py:199-246`). The model environment variables are not documented in `.env.example`.

No secret, password, token, or API-key configuration was found in `src/`, `.env.example`, `README.md`, `Dockerfile`, or `pyproject.toml`. `.env` is excluded by both `.gitignore` and `.dockerignore`, which prevents that file from being committed or copied by Docker. The repository contains no secret-management integration or required-secret inventory.

## 10. Health checks

There is no `/health`, readiness, or liveness endpoint. `tests/test_health.py` explicitly asserts that `/health` is not present in the OpenAPI paths; its test is an application import and `/openapi.json` smoke test. No Docker health check is provided.

Result: the repository does not provide a health contract suitable for orchestration evidence.

## 11. Upload limits and input handling

| Control | Implemented behavior | Limitation established by code |
| --- | --- | --- |
| Extension validation | Allows only AVI/MKV/MOV/MP4 suffixes | It is filename-suffix validation, not content-type validation (`video_validator.py:35-38`) |
| Content validation | OpenCV opens the stored file and reads one frame | Present (`video_validator.py:67-97`) |
| Size bound | 100 MiB default, counted while route reads 1 MiB chunks | The bound is applied while persisting the upload, after multipart form parsing (`routes.py:110-113`) |
| Duration bound | 900 seconds default from video metadata | Present (`video_validator.py:83-86`) |
| Dimension/FPS floor | 64x64 and 1.0 FPS defaults | Present (`video_validator.py:87-90`) |
| Disk quota | Per-session artifact reservation equals source-video size | No system-wide temp-disk quota or upload-directory policy is defined |

Malformed extensions, empty files, unreadable videos, invalid duration/FPS/dimensions, and oversized streaming reads are handled as `AnalysisError` and become HTTP 422. The repository contains no server/proxy body-size configuration; FastAPI form parsing occurs before the route's temporary-upload context and before admission. Consequently, an unadmitted request may have its multipart body parsed and then persisted before the process-local analysis capacity check.

## 12. Cleanup mechanisms

| Resource | Cleanup implemented | Evidence | Gap / guarantee |
| --- | --- | --- | --- |
| Upload temp file | `finally`: closes upload and unlinks named temp file | `src/services/video_validator.py:39-53` | Guaranteed by this context manager after entry; no explicit operating-system temp-volume quota |
| Validator `VideoCapture` | `finally: capture.release()` | `src/services/video_validator.py:69-78` | Guaranteed in validation |
| Player tracker `VideoCapture` | `finally: capture.release()` | `src/services/player_tracker.py:90-180` | Guaranteed in tracker analysis |
| Debug capture/writer | `finally` release behavior | `src/services/debug_renderer.py:36-...` | Present for inspected debug renderer |
| Camera-motion capture | Release occurs after its loop, not in `finally` | `src/services/camera_motion.py:74-...` | An exception before that statement bypasses release |
| Artifact session | Lifecycle `finally` invokes `artifacts.cleanup()` | `src/api/request_lifecycle.py:65-90` | Cleanup errors are returned by `ArtifactSession.cleanup()` and are not logged/raised by lifecycle |
| Artifact retention | Default retained sessions is zero; retained artifact sessions are removed under the manager's in-memory policy | `.env.example`; `src/diagnostics/artifacts.py:79-112` | No startup orphan-session scan is implemented |
| Admission permit / cancellation state | Nested `finally` completes cancellation and releases permit | `src/api/request_lifecycle.py:74-90` | No shutdown lifecycle invokes cancellation |

Debug artifact output is written by the renderer inside a retained session, while the artifact quota reservation shown in the route reserves and finalizes only the copied source video (`src/api/routes.py:210-214, 1050-1061`). The repository does not show quota reservation/finalization for debug-video or debug-frame output. Debug artifacts are disabled by default in `.env.example`, but enabling them can write unreserved outputs under the debug directory.

## 13. Security findings

| Severity | Finding | Repository evidence |
| --- | --- | --- |
| CRITICAL | Default Docker build omits the default ML model artifact and no alternate model provisioning is defined. Startup constructs both model adapters. | `Dockerfile`; root `yolo11n.pt`; `src/main.py:45-49`; `src/core/config.py:58-70` |
| HIGH | No health/readiness/liveness endpoint or container health check exists. | `tests/test_health.py`; `Dockerfile` |
| HIGH | The container has no non-root `USER` instruction. | `Dockerfile` |
| HIGH | Requests have no configured deadline; cancellation actions are not wired from HTTP disconnect, deadline, or shutdown. | `src/concurrency/cancellation.py`; `src/main.py`; `src/api/routes.py` |
| MEDIUM | Upload size is enforced during route persistence, but the multipart form is parsed before route admission and no server/proxy request-body limit is defined. | `src/api/routes.py:110-113`; `src/services/video_validator.py:39-49`; deployment files |
| MEDIUM | Supported input type is determined by filename suffix; content is checked only after disk persistence. | `src/services/video_validator.py:35-38, 67-97` |
| MEDIUM | Debug render outputs are not visibly covered by artifact reservations; no global disk quota or orphan cleanup is defined. | `src/api/routes.py:210-214, 1050-1061`; `src/diagnostics/artifacts.py` |
| MEDIUM | Application logging has no repository-configured structured format, request correlation policy, or cleanup/admission observability. | `src/core/logging.py`; `src/main.py`; `src/api/routes.py` |
| LOW | Camera-motion capture release is not protected by `finally`. | `src/services/camera_motion.py:74-...` |

No credentials were found in the inspected source/configuration files. The absence of credentials does not establish external transport, network, identity, host-hardening, or secret-store configuration; those are not represented in this repository.

## 14. Operational risks

| Severity | Risk | Exact behavior / evidence |
| --- | --- | --- |
| HIGH | Analysis capacity behavior is per process and rejects rather than queues. | `DEFAULT_MAX_ACTIVE_ANALYSES=1`; `AdmissionController.admit()` immediately returns `None` once active permits reach the limit (`src/config/analysis.py`; `src/concurrency/admission.py`) |
| HIGH | A long or stalled analysis occupies the sole process-local permit without an app deadline. | `RequestLifecycle` holds the permit until executor completion (`src/api/request_lifecycle.py`); no deadline invoker found |
| MEDIUM | Per-frame INFO inference logs can amplify log volume with video length. | Both YOLO adapter `detect` methods log each inference (`src/adapters/yolo_*_detector.py`) |
| MEDIUM | Deployment cannot prove container start behavior because CI does not build or run the image. | `.github/workflows/ci.yml` |
| MEDIUM | Current type-check gate fails. | `uv run mypy src tests` output recorded above; `.github/workflows/ci.yml` runs it |
| LOW | Built image size cannot be determined; no image artifact/build evidence exists. | Dockerfile and CI inspection |

## 15. Deployment blockers

1. **CRITICAL — Model provisioning is absent from the Docker build.** The documented build creates an image without the repository's default `yolo11n.pt`, while startup constructs both YOLO adapters using that path.
2. **HIGH — Health/readiness/liveness contract is absent.** `/health` is explicitly absent and Docker has no `HEALTHCHECK`.
3. **HIGH — No production request deadline/shutdown cancellation path is implemented.** The cancellation manager exists but no application route/lifespan code triggers it.
4. **HIGH — The repository's mandatory CI type-check command currently fails with nine errors.**
5. **HIGH — Docker process hardening is incomplete.** The image has no non-root user configuration.

## 16. Deployment checklist (evidence status)

| Check | Status |
| --- | --- |
| Test suite passes | Complete: 210 passed |
| Ruff lint and format checks pass | Complete |
| Mypy passes | Not complete: 9 errors |
| Model artifact available to the container by defined mechanism | Not complete |
| Container image built and exercised by repository CI | Not complete |
| Health, readiness, liveness contract available | Not complete |
| Request deadline and shutdown cancellation behavior verified | Not complete |
| Container non-root execution defined | Not complete |
| Upload limit enforced at deployment ingress | Not evidenced |
| Artifact/temp disk capacity and retention policy defined | Not complete |
| Structured/correlated production logging configured | Not complete |
| Reproducible Compose deployment | Not complete: no Compose file |

## 17. Final decision

## NO GO

Production deployment is blocked by the unprovisioned default model in the supplied image, absent health/readiness behavior, missing operational deadline/shutdown wiring, failing current mypy gate, and lack of non-root Docker configuration. The passing 210-test suite and clean Ruff results establish useful code-quality coverage, but they do not remove these deployability blockers.

### Files inspected

- `Dockerfile`, `.dockerignore`, `.gitignore`, `.env.example`, `README.md`, `pyproject.toml`, `.github/workflows/ci.yml`
- `src/main.py`, `src/api/routes.py`, `src/api/request_lifecycle.py`
- `src/core/config.py`, `src/core/logging.py`
- `src/concurrency/admission.py`, `src/concurrency/cancellation.py`, `src/concurrency/executor.py`
- `src/services/video_validator.py`, `src/services/player_tracker.py`, `src/services/camera_motion.py`, `src/services/debug_renderer.py`
- `src/adapters/yolo_player_detector.py`, `src/adapters/yolo_ball_detector.py`
- `src/diagnostics/artifacts.py`, `tests/test_health.py`, and relevant tests found by repository search.
