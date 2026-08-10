# Super-7 Backend Phase B1 Audit

## 1. Executive summary

This workspace is **not the Apex NestJS backend** described in the audit request. It is the Python/FastAPI Super-7 analysis service (`football-analysis`) plus a deliberately minimal integration test mock. Therefore, no authoritative backend facts about Apex users, players, video records, database tables, Nest modules, guards, or production routing can be derived here. Those items are explicitly marked **not represented in this repository**, rather than guessed.

The integration boundary is already implemented on the Super-7 side: a backend submits a shared-storage filename to `POST /analyze`, Super-7 returns a generated `analysisId` with HTTP 202, and it later sends a snake_case callback payload to the supplied public callback URL. Super-7 has no database client and does not persist backend-owned entities.

## 2. Audit scope and repository identity

- Inspected branch: `feature/phase-00-safety-hardening`.
- Inspected HEAD: `95470b869683e11783c28f76e1b4b53e799a630c` (`95470b8 Add backend integration handoff documentation outlining analysis workflow, responsibilities, and callback contract`, 2026-08-08 23:48:09 +03:00).
- Runtime: Python 3.12, FastAPI, Pydantic v2, Uvicorn; confirmed by `pyproject.toml`, `src/main.py`, and `Dockerfile`.
- Architecture: a Python modular monolith. `src/main.py:create_app` is the composition root, `src/api/routes.py` owns HTTP orchestration, and `src/services/analysis_queue.py` owns an in-memory FIFO queue with one worker.
- The only apparent backend is `integration/backend_mock/app.py`. It is a FastAPI test harness with in-memory dictionaries, not an Apex/NestJS backend or a production persistence implementation.

Relevant files inspected include `src/main.py`, `src/api/routes.py`, `src/api/health.py`, `src/schemas/analysis.py`, `src/services/analysis_queue.py`, `src/services/callback_service.py`, `src/services/video_path_resolver.py`, `src/core/config.py`, `src/core/logging.py`, `integration/backend_mock/app.py`, `.env`, `.env.example`, `Dockerfile`, `docker-compose.yml`, `docker-compose.integration.yml`, `pyproject.toml`, and `README.md`.

## 3. Existing backend architecture

There is no NestJS backend architecture to audit in this checkout:

- **Global API prefix:** none in Super-7. Its FastAPI routers register absolute routes such as `/analyze` and `/health/*`; no router prefix or application root path is configured in `src/main.py`, `src/api/routes.py`, or `src/api/health.py`.
- **Controllers/services/repositories:** FastAPI router functions and Python services exist; Nest controllers, services, modules, repositories, and DTO decorators do not.
- **Guards/interceptors/exception filters:** not represented. Super-7 uses FastAPI/Pydantic validation and route-level `HTTPException` handling.
- **Database technology, transactions, repositories, migrations:** none. `pyproject.toml` has no ORM/database dependency and source/configuration searches find no Prisma, TypeORM, Mongoose, SQLAlchemy, database connection, migration, or repository implementation.
- **Users and permanent players:** not represented. The only `playerId` is a backend-owned string echoed through the queue and callback. The analyzed `player.track_id` is a request-scoped ByteTrack ID, not a database player ID (`README.md`).

## 4. Existing video flow

The current production-facing flow is shared storage, not upload persistence:

1. A backend writes/retains a video in its shared video volume.
2. It sends JSON to Super-7 `POST /analyze` with `videoId`, `playerId`, `videoUrl`, and `callbackUrl` (`src/schemas/analysis.py:AnalyzeRequest`).
3. `src/api/routes.py:create_router` validates the filename reference and callback, generates a UUID4 `analysisId`, and enqueues an in-memory `AnalysisJob`.
4. Super-7 returns 202 with `analysisId`, `videoId`, `playerId`, and `status: "queued"`.
5. Its single worker resolves the stored filename, runs analysis, and posts a callback through `CallbackService`.

There is no Super-7 video-upload controller, no Super-7 video persistence model, and no durable upload directory. Historic local-upload/download helper code exists (`src/services/video_validator.py`, `src/services/video_downloader.py`), but the active `/analyze` route accepts only the shared-storage JSON contract.

## 5. Actual video storage path

The exact deployed Super-7 container path is **`/videos`**. `Settings.video_storage_root` defaults to `/videos`, `.env.example` sets `VIDEO_STORAGE_ROOT=/videos`, and both Compose files set/mount it.

`docker-compose.yml` maps host **`/data/videos`** to container **`/videos:ro`**. Thus the documented deployment mapping is:

| Layer | Actual path |
|---|---|
| Backend/deployment host shared volume | `/data/videos` |
| Super-7 container | `/videos` (read-only) |
| Request field | safe relative basename, for example `test-video.mp4` |

This is Super-7 deployment evidence only; it does not establish Apex's current upload directory. `VideoPathResolver` rejects absolute paths, path separators, traversal, unsupported extensions, and escaping symlinks. **The backend must not submit an absolute backend path.**

## 6. Player and video persistence models

No backend persistence model is available in this repository.

| Requested item | Finding |
|---|---|
| Video primary key | Not represented; `videoId` is a required unconstrained `str` supplied by the backend. |
| Video-to-player relation | Not represented; `playerId` is a required unconstrained `str` supplied by the backend. |
| Video path/URL database field | Not represented. Super-7 receives `videoUrl` as a safe shared-storage filename. |
| Video analysis/status fields | Not represented in a database. Super-7 queue states are process-local only. |
| Player entity and exact ID type | Not represented. Super-7's API type is string; this is not evidence of the Apex entity ID type. |
| Existing analysis table/entity | No. There is no database or analysis table/entity in this repository. |

The only analysis-job object is `services.analysis_queue.AnalysisJob`: `analysis_id`, `video_id`, `player_id`, `video_reference`, `callback_url`, and `submitted_at`. It is a frozen Python dataclass held in an `asyncio.Queue` and in-memory state dictionary, not persisted.

## 7. Existing analysis capabilities and current AI integration

Super-7 is itself the AI analysis service. It exposes `POST /analyze`, uses a bounded process-local FIFO queue, and calls a backend callback through standard-library HTTP (`urllib.request`), not Axios, Nest `HttpService`, or `fetch`.

- Existing Super-7 client in the backend: **not represented / cannot be verified**. There is no Apex backend here.
- Existing Super-7 server endpoint: **yes**, `POST /analyze`.
- Existing backend webhook convention: only the test mock has `POST /webhook`; it is not a production convention.
- Super-7 statuses: `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED` as `StrEnum` values in `src/services/analysis_queue.py`. The immediate external status is lowercase `queued`; callback success is normally lowercase `completed`.
- Super-7 analysis result is not durably stored. It is converted to callback `summary`, `ratings`, and `events` maps and delivered to the backend.

## 8. Global routing and recommended webhook location

Super-7 has no API prefix and its submission endpoint is exactly:

```text
POST /analyze
```

The Apex backend's global prefix and module routes are not present, so an **exact existing backend webhook path cannot be derived**. Conditional recommendation, pending the Apex audit: if its global prefix is `/api` and ownership is the existing video/analysis capability, use:

```text
POST /api/video-analysis/webhook
```

If no `video-analysis` route/module exists, place the webhook under the existing video-processing/upload feature rather than automatically creating a parallel module. Do not implement that endpoint until the Apex repository is supplied and its prefix/module boundaries are verified.

The exact recommended Super-7 submission location is the backend's existing service that transitions a persisted video into analysis/processing state. In this repository, the receiving location is exactly `POST /analyze`; no backend submission service exists here to name more precisely.

## 9. Validation, errors, and logging

Super-7 uses Pydantic v2 models. `AnalyzeRequest` has `ConfigDict(extra="forbid", populate_by_name=True)`, so unknown request fields are rejected and accepted request names are camelCase aliases. FastAPI returns request-validation failures as HTTP 422. Route-specific invalid callback/reference failures also return HTTP 422; a full queue returns HTTP 503.

There is no Nest `ValidationPipe`, class-validator, Zod, global exception filter, or DTO layer in this checkout. Backend validation conventions cannot be determined.

Logging uses the Python standard library `logging` through `core.logging.get_logger`. It emits named logger events such as `analysis_job_queued`, `analysis_job_started`, `callback_delivered`, and `callback_delivery_failed`. No repository-level structured logging formatter or correlation middleware is configured. `analysisId`/callback `request_id` is the available correlation key.

## 10. Deployment, networking, and environment

- **Port:** Super-7 runs on **8000** (`Dockerfile` Uvicorn command; Compose mapping `8000:8000`; README). It is not configured for port 3001.
- **Dockerized:** yes. `Dockerfile`, `docker-compose.yml`, and `docker-compose.integration.yml` are present.
- **Nginx:** no Nginx configuration is represented in this repository.
- **Public backend domain:** none is documented. The handoff uses illustrative `backend.example.com`; it is not a configured production domain.
- **`AI_SERVICE_URL`:** absent from `.env`, `.env.example`, and source configuration.
- **`AI_WEBHOOK_CALLBACK_URL`:** absent from `.env`, `.env.example`, and source configuration.
- **Callback networking constraint:** callback URLs must be HTTP(S) and resolve only to global/public IP addresses; private/loopback Docker hosts and redirects are rejected (`CallbackService`).

## 11. Data ownership map

| Data/state | Current owner | Persistence location | Exists now? | Future integration owner |
|---|---|---|---|---|
| User | Apex backend, by intended design | Not represented here | No repository evidence | Existing Apex user/auth domain |
| Player | Apex backend, by intended design | Not represented here | No repository evidence | Existing Apex player domain |
| Video | Apex backend/deployment | Host shared volume `/data/videos`; Super-7 reads `/videos` | Shared-storage contract exists; backend DB record absent | Existing Apex video/upload domain |
| Analysis job | Super-7 | In-memory `asyncio.Queue` and state dict | Yes, non-durable | Apex should persist correlation/pending state; Super-7 retains execution queue |
| AI result | Super-7 while processing; Apex after callback | No durable Super-7 store; callback JSON | Computed and callback-delivered | Existing Apex video/analysis persistence boundary |
| Webhook | Apex backend | Not represented; mock only stores memory | Production endpoint absent | Existing Apex video-processing/analysis controller |
| Super-7 `analysisId` | Generated by Super-7 | In-memory only on Super-7 | Yes | Apex must persist it against its analysis/video record for callback correlation |

## 12. Integration gap analysis

| Capability | Classification | Evidence and required outcome |
|---|---|---|
| Apex repository audit | REQUIRED NOW | This checkout cannot answer backend-model, route, or module questions. Supply/access the Apex repository before B2. |
| Shared readable video volume | REQUIRED NOW | Super-7 requires `/videos` mapped from backend-accessible storage; submit a relative basename. |
| Public callback URL | REQUIRED NOW | Must be HTTP(S), externally reachable, and resolve only to global IP addresses. |
| Backend persistence of `analysisId`, video/player IDs, request state | REQUIRED NOW | Super-7's queue is non-durable and callback uses `request_id`. |
| Backend webhook endpoint and callback DTO | REQUIRED NOW for end-to-end integration | Implement in the verified existing Apex video/analysis boundary. |
| Idempotent callback persistence | REQUIRED NOW | Super-7 can retry callback delivery and duplicate submissions are not deduplicated. |
| Submission HTTP client and state transition | REQUIRED NOW | Add in Apex's existing video-processing flow after storage is complete. |
| Analysis result fields/migration | REQUIRED LATER | Depends on the actual Apex database schema and desired reporting/query requirements. |
| Retry/reconciliation policy | REQUIRED LATER, but design now | Super-7 has no durable queue/status/replay endpoint; Apex must reconcile pending records and deliberately resubmit. |
| Super-7-side database, Redis, Kafka, RabbitMQ, BullMQ, microservices, WebSockets | NOT REQUIRED | No repository evidence requires these additions. |

## 13. Risks

- Treating this Super-7 repository as Apex would create duplicated models/modules and invalid assumptions about IDs, database ownership, and routing.
- A 202 response only means queued; it does not mean the analysis completed or the callback was delivered.
- Queue/job state is in-memory and is lost/cancelled on shutdown; the backend must retain pending state and source video.
- Callback validation rejects private Docker, localhost, and mixed/public-private DNS targets, so a production ingress/domain is required.
- Submitting a backend absolute path will fail. The basename must resolve inside Super-7's configured read-only volume.
- There is no evidence of Super-7 request deduplication; backend callback writes and submission policy must be idempotent.

## 14. Exact Phase B2 scope

Phase B2 must begin by auditing the actual Apex NestJS repository and then, only after confirming its existing module/persistence boundaries:

1. Identify the real API prefix, video upload/storage implementation, video/player entities and ID types, database stack, existing analysis state, validation, and controller conventions.
2. Select the existing Apex service that owns the post-upload/process-state transition; do not create a duplicate module unless the audit proves there is no suitable boundary.
3. Define a minimal durable analysis correlation/status record or extend the existing video-analysis persistence according to the actual schema; persist returned `analysisId`.
4. Add the Super-7 submission client for `POST /analyze` using the relative shared-storage filename and configured public callback URL.
5. Add the idempotent callback DTO/controller at the verified Apex route, persist terminal result/error, and test success, retry, duplicate-callback, missing-video, and failed-analysis paths.
6. Configure shared storage and public ingress; do not add unrelated brokers, caches, databases, or service splits.

No production code, database, endpoint, DTO, or infrastructure was modified by this Phase B1 audit.
