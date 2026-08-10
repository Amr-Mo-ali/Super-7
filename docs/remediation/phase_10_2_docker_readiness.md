# Phase 10.2 — Docker deployment readiness remediation

## 1. Objective

Remediate Docker-only deployment blockers without changing application algorithms, scoring, tracking, event arbitration, Public Rating V2, concurrency semantics, health endpoints, request deadlines, or shutdown orchestration.

## 2. Addressed blockers

| Previous blocker | Resolution |
| --- | --- |
| Docker image did not contain the configured model | The image now has an explicit external model-volume contract; models are deliberately excluded from the image. |
| Model provisioning was undefined | `docker-compose.yml` mounts `./models` read-only at `/models`; `.env.example` defines `MODEL_PATH` and `BALL_MODEL_PATH` as `/models/yolo11n.pt`. |
| No Docker `HEALTHCHECK` | The Dockerfile and Compose service check the already-existing `/openapi.json` endpoint. No endpoint was added. |
| No `EXPOSE` instruction | Dockerfile declares `EXPOSE 8000`. |
| No non-root user | Dockerfile creates and runs as the `app` system user. |
| No Compose definition | Added `docker-compose.yml` with build, ports, `.env`, read-only model mount, restart policy, init process, and health check. |
| CI did not build the image | CI now runs `docker build --tag football-analysis:ci .` after quality checks. |

## 3. Selected model strategy

The selected strategy is **host volume -> environment path -> container**.

```text
host ./models/yolo11n.pt
        |
        +-- read-only bind mount --> /models/yolo11n.pt
                                           |
                                           +--> MODEL_PATH / BALL_MODEL_PATH
                                                    |
                                                    +--> YOLO adapters at application startup
```

This is the smallest explicit mechanism available in the repository: it requires no cloud service, no download behavior, no startup redesign, and no model binary in the image build context. The image can therefore be built independently of model availability; a deployed service starts only when the configured model file is present in the host-mounted directory.

The pre-existing tracked root `yolo11n.pt` working file was relocated to `models/yolo11n.pt`. `.gitignore` prevents future `yolo11n.pt` and `models/*.pt` additions, while `.dockerignore` excludes both model locations from the build context. This does not rewrite historical Git commits.

## 4. Files changed

| File | Change |
| --- | --- |
| `Dockerfile` | Added runtime environment defaults, system `app` user, ownership-aware copies, app/debug/model directories, non-root execution, `EXPOSE 8000`, OpenAPI-based health check, and retained exec-form Uvicorn startup. |
| `.dockerignore` | Excluded `models/` and root `yolo11n.pt` from build context. |
| `.gitignore` | Prevented future root/default and `models/*.pt` model additions. |
| `.env.example` | Documented the default container model paths. |
| `docker-compose.yml` | Added reproducible local service definition. |
| `models/README.md` | Documented the host-volume model placement contract. |
| `.github/workflows/ci.yml` | Added a Docker image build verification step. |
| `README.md` | Replaced skeleton-image instructions with the Compose deployment procedure. |
| `yolo11n.pt` | Moved from the tracked repository root to ignored `models/yolo11n.pt`. |

## 5. Dockerfile changes

- Uses `python:3.12-slim`, preserving the existing Python base version.
- Sets unbuffered output and `PYTHONPATH=/app/src`.
- Creates a system `app` user before copying source and uses `COPY --chown=app:app`.
- Installs the project without pip cache, creates writable `/app/debug`, and prepares `/models` as the mount target.
- Runs Uvicorn as `app` using its JSON/exec form. This preserves direct signal delivery to Uvicorn and does not add a shell wrapper.
- Declares port 8000 and an image health check against `http://127.0.0.1:8000/openapi.json`.

The health check reuses an existing endpoint specifically because Phase 10.2 does not add a health endpoint. It confirms that the process has started and can serve the OpenAPI document; it is not a new readiness contract.

## 6. Compose changes

`docker-compose.yml` defines one `football-analysis` service:

- build context `.` and local image name `football-analysis:local`;
- port mapping `8000:8000`;
- required `.env` file (created by copying `.env.example`);
- `./models:/models:ro` bind mount;
- `restart: unless-stopped`;
- `init: true`;
- the same OpenAPI-based health check and timing as the image.

The source model directory is mounted read-only. The container receives model paths from the environment rather than any host-specific path.

## 7. CI changes

The existing quality job now includes:

```text
docker build --tag football-analysis:ci .
```

It builds only; it does not deploy or attempt model-backed service startup. Models are intentionally not in the build context.

## 8. Verification

| Check | Result |
| --- | --- |
| `uv run pytest -q` | Passed: 210 passed in 6.40s |
| `uv run mypy src tests` | Passed: no issues in 138 source files |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check .` | Passed: 182 files already formatted |
| Local app startup with `MODEL_PATH` and `BALL_MODEL_PATH` set to `models/yolo11n.pt` | Passed; printed `Football Analysis MVP` |
| `docker build --tag football-analysis:phase-10-2 .` | **Blocked**: Docker Desktop Linux daemon was unavailable at `//./pipe/dockerDesktopLinuxEngine` |
| Container startup and container health-check execution | **Not run**: blocked by the unavailable Docker daemon |
| `docker compose --env-file .env.example config` | Did not validate because the Compose service correctly requires its `env_file: .env`, which has not been created in this workspace. README documents `Copy-Item .env.example .env` before Compose use. |

The failed Docker command is an environment availability failure, not a reported Dockerfile build error. A host with Docker running must execute the documented `docker compose up --build` verification before the container build/start exit criteria can be marked complete.

## 9. Risks and limits

- Host-volume deployment requires the operator to provide readable model files under `./models` before service startup.
- The Compose health check is an application-start check against `/openapi.json`; it is not a dedicated liveness or readiness API.
- Container build, container startup, and health-check execution were not verifiable in this environment because no Docker daemon is running.
- This phase does not add ingress upload limits, request deadlines, shutdown cancellation, or a health endpoint; those remain outside its permitted scope.

## 10. Next phase

**Phase 10.3 — Health and lifecycle readiness remediation.** Define and verify dedicated health/readiness behavior and the remaining request deadline/shutdown lifecycle blockers identified by the pre-deployment readiness audit.

