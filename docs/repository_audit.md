# Repository Audit — Super-7 Football Analysis

## Scope and evidence standard

This report is a read-only audit of the repository at the revision below. Statements are based on source, configuration, and tests inspected in this repository. Deployment behaviour outside the checked-in files, production traffic, cloud resources, secrets, public DNS, and operational runbooks are **NOT REPRESENTED IN THIS REPOSITORY**.

## Repository overview

| Item | Evidence-based finding |
|---|---|
| Repository/package name | `football-analysis` (`pyproject.toml`) |
| Branch | `feature/phase-00-safety-hardening` |
| Latest commit | `717ad438cfb90f1550a4490fafc61bd11f53754e` — `717ad43 Add Super-7 Backend Phase B1 Audit documentation outlining architecture, integration scope, and existing capabilities` (2026-08-09 17:42:28 +03:00) |
| Language/runtime | Python, requires `>=3.12`; CI selects Python 3.12 (`pyproject.toml`, `.github/workflows/ci.yml`) |
| Framework | FastAPI `>=0.115,<1`, Pydantic `>=2,<3`, Uvicorn `>=0.30,<1` |
| Package/dependency manager | `uv`; locked dependencies in `uv.lock`; CI uses `uv sync --frozen --all-groups` |
| Application type | Python FastAPI modular monolith implementing asynchronous football-video analysis, not an Apex/NestJS backend |
| Database | **NOT FOUND.** No database driver/ORM/migration/deployment configuration is represented. |

## Architecture

`src/main.py:create_app` is the composition root. It creates immutable runtime settings; detector adapters; request lifecycle/admission/executor/artifact services; a shared-storage resolver; callback delivery; a bounded queue; and one queue worker. It includes the health router and analysis router in a `FastAPI` application.

```text
Backend-owned video in shared storage
  -> POST /analyze
  -> Pydantic/reference/callback validation
  -> in-memory asyncio.Queue
  -> one AnalysisWorker
  -> RequestLifecycle admission + deadline + artifact session
  -> synchronous CV/ML pipeline in asyncio.to_thread
  -> CallbackService POST to backend-owned callback URL
```

Ownership boundaries are explicit in implementation:

- Super-7 owns request validation, queueing, model execution, video analysis, callback attempts, local debug artifacts, and process-local lifecycle state.
- The caller owns `videoId`, `playerId`, source-video persistence, durable job/result state, the callback endpoint, and any user/player database model. These are **NOT REPRESENTED IN THIS REPOSITORY**.
- `integration/backend_mock` is a FastAPI test harness. Its lists/dictionaries are in memory and are not production persistence.

The main dependency graph flows from `api.routes` through services/adapters/configuration. `api.routes` is the HTTP orchestration boundary; core services contain reusable analysis/lifecycle logic; `schemas` owns Pydantic contracts; `diagnostics` owns per-request artifact and timing helpers; `domain` contains value-model logic for possession/timeline/sequences/transitions.

## Project structure

```text
src/
├── main.py                         # FastAPI composition root and lifespan
├── api/                            # /analyze, health, request lifecycle, public mapping
├── adapters/                       # YOLO player and ball detector adapters
├── concurrency/                    # admission, execution thread boundary, cancellation
├── config/                         # feature/debug/profile configuration
├── core/                           # Settings, exceptions, logging, reproducibility
├── diagnostics/                    # request artifact lifecycle and performance collector
├── domain/                         # possession, timeline, sequences, transitions models
├── schemas/                        # Pydantic API/result contracts
└── services/                       # queue, callbacks, storage, CV/ML analysis/scoring
    ├── event_arbitration/
    ├── interactions/
    ├── movement/
    ├── player_rating/
    ├── scoring/
    └── technical_events/

integration/backend_mock/           # test-only callback receiver
tests/                              # unit, API/lifecycle, concurrency and integration tests
docs/                               # audits, remediation records, integration handoff
Dockerfile                          # production image definition
docker-compose.yml                  # local/Compose deployment
docker-compose.integration.yml      # Super-7 + backend mock verification harness
.github/workflows/ci.yml            # CI quality/test/image build workflow
```

`src/football_analysis` contains package-directory placeholders/cache directories and no feature source files. `dataset/` contains sample/raw dataset layout and `models/` contains a model-readme plus local model artifact; neither is included in Docker build context (`.dockerignore`).

## HTTP API audit

No global prefix, router prefix, API-version prefix, or root-path configuration is set in `src/main.py`, `src/api/routes.py`, or `src/api/health.py`.

| Method/path | Owner | Request | Response/status | Exceptions/behaviour |
|---|---|---|---|---|
| `POST /analyze` | `api.routes:create_router` | JSON `AnalyzeRequest`: required camelCase `videoId: str`, `playerId: str`, `videoUrl: str`, `callbackUrl: HttpUrl`; unknown fields forbidden | `AnalyzeQueuedResponse`; 202 `{analysisId, videoId, playerId, status:"queued"}` | 422 if reference/callback validation raises; 503 if queue is stopped/full. File existence is checked later by worker, not before the 202. |
| `GET /health/live` | `api.health:create_health_router` | None | JSON, 200 if startup/detector/configuration flags are true; otherwise 503 | Checks app state only. |
| `GET /health/ready` | `api.health:create_health_router` | None | JSON, 200 only when all readiness checks pass; otherwise 503 | Checks admission, queue capacity/worker, lifecycle, artifact manager, app model flag, temp directory, and video storage root checks. |
| `GET /health` | `api.health:create_health_router` | None | Combined live/ready JSON, 200 only if both sets pass; otherwise 503 | Same component checks as above. |

FastAPI generates OpenAPI at its framework defaults; `/openapi.json` is exercised by Docker health checks and tests. A custom OpenAPI versioning policy is **NOT FOUND**. The analysis response schema is intentionally asynchronous: `CompletedResponse`/`NonCompletedResponse` are used internally to build callbacks, while `/analyze` only returns `AnalyzeQueuedResponse`.

Test-only mock endpoints (`integration/backend_mock/app.py`) are `GET /health/live`, `POST /webhook` (204), `GET /callbacks`, and `DELETE /callbacks` (204). They are not production routes of Super-7.

## Request, processing, and callback lifecycle

```text
POST /analyze
  -> Pydantic v2 parsing (extra fields forbidden)
  -> VideoPathResolver.validate_reference (format only)
  -> CallbackService.validate_callback_url (scheme/DNS-public-IP checks)
  -> AnalysisJob.create(UUID4 analysisId)
  -> AnalysisQueue.submit
  -> HTTP 202
  -> one AnalysisWorker dequeues and marks RUNNING
  -> VideoPathResolver.resolve (existence, regular file, containment, read access)
  -> RequestLifecycle executes analysis in a worker thread
  -> callback payload built from public rating projection
  -> CallbackService delivers JSON or logs exhausted delivery failure
  -> in-memory terminal state: COMPLETED/FAILED/CANCELLED
```

The processing pipeline includes video validation, player/ball detection and tracking, target selection, trajectory/interaction/movement analysis, technical events, pass/shot detection, scoring, public-rating mapping, and optional debug rendering. The concrete orchestration is `_analyze_uploaded` in `src/api/routes.py`; component construction occurs in `src/main.py`.

## Callback audit

Callback payload is `CallbackPayload` in `src/services/callback_service.py`: `request_id`, `video_id`, `player_id`, `status`, `summary`, `ratings`, `events`, and nullable `error`. It is JSON POSTed with `Content-Type: application/json` and `Accept: application/json`.

- Success is any HTTP 2xx response.
- Retry policy: initial attempt plus three retries after 1, 2, and 4 seconds (four attempts total).
- Per-attempt timeout: `CALLBACK_TIMEOUT_SECONDS`, default 10 seconds.
- Failure policy: delivery failures are logged and do not turn a successfully processed job into a failed queue state. Pipeline failure attempts a sanitized `status:"failed"` callback; failed delivery is logged.
- Idempotency: **NOT IMPLEMENTED.** Callback retry can deliver the same payload more than once; no idempotency key beyond `request_id` is enforced by Super-7.
- Callback security: destination must be HTTP or HTTPS, have a host, resolve only to global/public IP addresses, and may not redirect. This rejects loopback, private, link-local, and mixed-address DNS answers. DNS is validated before admission and again before delivery.
- Public webhook support: **YES, conditionally.** A callback URL is accepted only when it meets the public-IP DNS and HTTP(S) restrictions. Authentication/signing of callback payloads is **NOT FOUND**.

## Storage audit

Super-7 does not persist user, player, video, analysis-job, or AI-result database records. Shared input storage is configured through `VIDEO_STORAGE_ROOT`, default `/videos` (`Settings`). Production Compose mounts host `/data/videos` to container `/videos:ro`.

`VideoPathResolver` accepts only a non-empty, unpadded relative basename with extension `.mp4`, `.mov`, `.mkv`, or `.avi`. It rejects absolute POSIX/Windows paths, separators, traversal, and symlink escape. On worker execution it resolves the path strictly, requires containment under the root, a regular readable file, and a readable/searchable storage root.

Temporary/debug artifacts are owned by `diagnostics.artifacts.ArtifactManager` under `debug_output_dir`, default `debug`. Artifact sessions are request-scoped, name-validated, quota-reserved against `MAX_UPLOAD_BYTES`, and cleaned in the request lifecycle `finally` path unless the configured retention policy keeps them. System-wide disk quota, remote/object storage, database persistence, and backup/retention operations are **NOT REPRESENTED IN THIS REPOSITORY**.

## Concurrency and lifecycle audit

- Queue: bounded process-local `asyncio.Queue[AnalysisJob]`, default capacity 10 (`MAX_QUEUED_ANALYSES`).
- Worker: exactly one lifespan-owned `AnalysisWorker` task consumes the queue.
- Active work: `AdmissionController` defaults to one active analysis (`DEFAULT_MAX_ACTIVE_ANALYSES = 1`) and protects counters with `threading.Lock`.
- Threading: the synchronous pipeline and outgoing callback transport use `asyncio.to_thread`; the application event loop runs queue/lifecycle coordination.
- Locks: `threading.Lock` protects admission state, cancellation state, lifecycle active map, and artifact-manager state. No distributed lock is present.
- Cancellation: a request-scoped `CancellationManager` uses a `threading.Event`; cancellation is cooperative at explicit `CancellationChecker` stage boundaries. It does not force-interrupt native OpenCV/YOLO work.
- Deadline: `RequestLifecycle` starts an asyncio deadline task for `REQUEST_DEADLINE_SECONDS`, default 900 seconds, when worker execution begins—not while a job waits in the queue.
- Shutdown: FastAPI lifespan stops queue admission, requests cooperative cancellation for active lifecycle work, awaits its completion, and cancels the worker task. Waiting queue jobs are marked `CANCELLED`; no durable recovery is implemented.

## Configuration audit

`.env.example` documents the primary operational configuration. `Settings.from_environment` also reads model and analysis tuning values. Values absent from environment use code defaults.

| Variable | Default/evidence | Purpose |
|---|---:|---|
| `VIDEO_STORAGE_ROOT` | `/videos` | Shared, read-only input root |
| `MAX_QUEUED_ANALYSES` | `10` | Bounded in-memory queue capacity |
| `REQUEST_DEADLINE_SECONDS` | `900` | Cooperative execution deadline |
| `CALLBACK_TIMEOUT_SECONDS` | `10` | Per callback attempt timeout |
| `MAX_UPLOAD_BYTES` | `104857600` | Validation/artifact session size bound |
| `MAX_DURATION_SECONDS` | `900` | Video validation bound |
| `MODEL_PATH`, `BALL_MODEL_PATH` | `yolo11n.pt` (Compose `.env` uses `/models/yolo11n.pt`) | YOLO model artifacts |
| `MODEL_DEVICE` | `cpu` | Detector device |
| `MODEL_CONFIDENCE`, `MODEL_IOU`, `MODEL_IMAGE_SIZE` | `0.25`, `0.45`, `640` | Player detector tuning |
| `BALL_CONFIDENCE`, `BALL_IOU`, `BALL_IMAGE_SIZE` | `0.15`, `0.45`, `640` | Ball detector tuning |
| `TARGET_SELECTION_MODE` and target/segment/tracklet values | code defaults | Player/segment selection tuning |
| `DEBUG_ARTIFACTS_ENABLED`, `DEBUG_SAVE_VIDEO`, `DEBUG_SAVE_FRAMES`, `DEBUG_SAVE_ON_FAILURE`, `DEBUG_RETAINED_SESSIONS` | false/false/false/false/0 in `.env.example` | Debug artifact output/retention |

`AI_SERVICE_URL`, `AI_WEBHOOK_CALLBACK_URL`, secret/API-key variables, and a production public-backend domain are **NOT FOUND** in source, `.env`, or `.env.example`.

## Security audit

| Area | Finding |
|---|---|
| SSRF/callback destination | Present: callback scheme/host/DNS global-IP validation and redirect rejection (`CallbackService`). DNS rebinding between validation and connection is not separately mitigated in code. |
| Path traversal | Present: basename-only validation, strict path resolution, containment and symlink-escape checks (`VideoPathResolver`). |
| API request validation | Present: Pydantic v2 with `extra="forbid"`; required request fields and `HttpUrl` callback type. |
| Video validation | Present in services: filename suffix restrictions and OpenCV metadata/content validation. Active shared-storage route resolves file during worker processing. |
| Authentication/authorization | **NOT FOUND.** No auth middleware, API key, JWT, OAuth, roles, or authorization checks are represented. |
| Callback authentication/integrity | **NOT FOUND.** No signature, shared secret, mTLS, or replay protection is implemented. |
| CORS | **NOT FOUND.** No CORS middleware configuration is represented. |
| Rate limiting/request quotas | **NOT FOUND.** Queue capacity limits accepted jobs but is not an HTTP rate limiter. |
| Container user | Present: Dockerfile creates and runs as non-root `app`. |
| Secrets management | **NOT FOUND.** `.env` is git/docker ignored, but no secret store, secret inventory, or required secret is configured. |

## Observability audit

Python standard-library named logging is exposed through `core.logging.get_logger`. Source logs queue admission/start/terminal events, callback delivery/rejection/failure, startup detector configuration, and many pipeline exceptions. `analysisId`/callback `request_id` appears in relevant queue/callback log messages.

`diagnostics.performance` collects per-request timing data used in analysis output; it is not an external metrics exporter. Admission and queue metrics are in-memory snapshots used for logic/readiness.

- Metrics endpoint/Prometheus/OpenTelemetry exporter: **NOT FOUND**.
- Distributed tracing/request tracing: **NOT FOUND**.
- Log formatter, structured log sink, retention, alerting, dashboards: **NOT REPRESENTED IN THIS REPOSITORY**.
- Health/readiness: implemented as `/health/live`, `/health/ready`, and `/health`.

## Performance audit

The dominant work is synchronous OpenCV/YOLO analysis. It is dispatched to a worker thread, while a single application worker and one admission permit serialize active analysis by default. Video decoding, frame processing, model inference, source-video copying/debug rendering, hashing/reproducibility operations, and callback network I/O are the documented code paths with material CPU, memory, disk, or network use.

The repository has a `scripts/benchmark_analysis.py` script and stored benchmark/debug output. Reproducible production throughput, memory limits, GPU allocation, autoscaling thresholds, load-test results, and capacity data are **NOT REPRESENTED IN THIS REPOSITORY**. Compose defines no CPU, memory, GPU, or replica limits.

## Testing and CI audit

Tests are organized by API/lifecycle, concurrency, diagnostics, integration, feature/scoring components, and video I/O. Examples include `tests/test_analysis_queue.py`, `tests/test_callback_service.py`, `tests/api/test_request_lifecycle.py`, `tests/concurrency/*`, `tests/diagnostics/*`, and `tests/integration/test_phase_11_6_backend_flow.py`.

The integration test uses an in-process/mock callback database and injected dependencies; it does not prove a real database, public DNS, public ingress, or external callback endpoint. Unit tests cover callback retry/rejection, path resolver restrictions, queue states, cancellation/lifecycle, artifacts, health, schemas, and analysis components.

CI (`.github/workflows/ci.yml`) runs on push and pull request with read-only `contents` permission. It performs locked dependency installation, Ruff lint/format checks, mypy, pytest (with model paths intended not to load production models), then `docker build`. Deployment, image publishing, SBOM/vulnerability scanning, secret scanning, signed releases, and CD are **NOT FOUND**.

## Deployment audit

The Docker image is Python 3.12 slim, runs as the non-root `app` user, sets `PYTHONPATH=/app/src`, exposes 8000, and starts `uvicorn main:app --host 0.0.0.0 --port 8000`. The container health check probes `/openapi.json`.

`docker-compose.yml` publishes `8000:8000`, loads `.env`, mounts `./models:/models:ro` and `/data/videos:/videos:ro`, uses `restart: unless-stopped` and `init: true`, and checks `/openapi.json`. The integration Compose file adds `backend-mock` on host `8081` to container `8080`, and Super-7 on 8000.

Kubernetes manifests, Helm charts, Nginx configuration, reverse-proxy configuration, TLS configuration, public DNS configuration, multi-replica configuration, and a production CD deployment are **NOT FOUND**.

## Integration readiness answers

| Question | Answer |
|---|---|
| Is the system production-ready? | **No, based on repository evidence.** Durable work/result state, authentication, callback integrity, monitoring/tracing, reconciliation, and deployment-level public ingress/capacity evidence are absent. |
| Is it horizontally scalable? | **No.** Queue/admission/lifecycle state are process-local; no shared queue/lock/state coordination exists. |
| Is the queue durable? | **No.** It is an in-memory `asyncio.Queue`. |
| Can duplicate callbacks occur? | **Yes.** Retried delivery has no receiver-side idempotency guarantee. |
| Can duplicate requests occur? | **Yes.** No request deduplication/idempotency mechanism is implemented. |
| Can jobs survive restarts? | **No.** Waiting jobs are cancelled/drained and queue state is process-local. |
| Is reconciliation implemented? | **No.** No durable status, replay, or reconciliation endpoint/job exists. |
| Is authentication implemented? | **No; NOT FOUND.** |
| Are metrics implemented? | **No external metrics implementation found.** In-process timing/capacity snapshots exist. |
| Is tracing implemented? | **No; NOT FOUND.** |
| Is rate limiting implemented? | **No; NOT FOUND.** Queue capacity is not rate limiting. |
| Is API versioning implemented? | **No route/versioning scheme found.** FastAPI app metadata has `analysis_version`. |
| Is OpenAPI implemented? | **Yes.** FastAPI default OpenAPI is available and `/openapi.json` is used by tests/health checks. |
| Is a public webhook supported? | **Yes, conditionally.** The callback URL must resolve solely to public IPs and use HTTP(S); no callback authentication is present. |

## Risk analysis

| Severity | Evidence-based issue | Consequence |
|---|---|---|
| CRITICAL | No authentication or authorization is implemented for `POST /analyze`. | Any network-reachable caller can submit work, subject only to validation and queue capacity. |
| CRITICAL | Callback payload has no signature, shared secret, mTLS, or replay protection. | A receiver cannot authenticate Super-7 callbacks from repository-provided data alone. |
| HIGH | Queue, job state, and callback delivery state are process-local/non-durable. | Restart/shutdown can lose/cancel queued work; no automatic recovery or reconciliation exists. |
| HIGH | No durable idempotency mechanism for submissions or callbacks. | Duplicate analysis work and duplicate result persistence are possible. |
| HIGH | Single worker/default single active analysis with no distributed coordination. | The service is not horizontally scalable and has limited throughput. |
| HIGH | No production metrics, tracing, dashboards, or alerting are represented. | Operational detection, capacity management, and incident diagnosis are limited. |
| MEDIUM | Callback policy accepts public HTTP as well as HTTPS. | Transport security depends on caller configuration; repository does not require TLS. |
| MEDIUM | Cooperative cancellation cannot force-stop native synchronous CV/ML work. | Deadline/shutdown latency can exceed the configured deadline until a checkpoint is reached. |
| MEDIUM | Container Compose has no checked-in resource limits, GPU configuration, autoscaling, or production ingress/TLS configuration. | Capacity and deployment behaviour cannot be verified from the repository. |
| MEDIUM | The 202 acknowledgement precedes file existence/readability validation. | A caller can receive accepted/queued and later only receive a failed callback if the file is missing. |
| LOW | Health checks set application state flags during composition; model-load probing and external dependency checks are not independently evidenced. | Readiness may not fully establish inference availability. |
| LOW | Docker health check probes OpenAPI, while richer readiness checks exist separately. | Container health does not exercise `/health/ready`. |

## Final assessment

The repository provides a clearly structured single-process Super-7 analysis service with strong request-shape, callback-destination, and shared-path controls; lifecycle, cancellation, artifacts, and callback retries are implemented and tested. It intentionally delegates durable ownership to an external backend, but that backend is **NOT REPRESENTED IN THIS REPOSITORY**. The missing durability, idempotency, authentication, callback integrity, production observability, and multi-instance coordination prevent an evidence-based production-ready conclusion.
