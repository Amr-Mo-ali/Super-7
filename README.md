# Football Analysis

Python 3.12 modular-monolith MVP exposing one endpoint: `POST /analyze`.

## Prerequisites

- [uv](https://docs.astral.sh/uv/)
- Python 3.12 (uv installs it automatically when needed)

## Local setup

```powershell
uv python install 3.12
uv sync --all-groups
uv run pre-commit install
```

Copy environment defaults only when the application later requires them:

```powershell
Copy-Item .env.example .env
```

## Development commands

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest
uv run pre-commit run --all-files
```

## Durable persistence development

Persistence is disabled by default. For an isolated Super-7 development database only, set
`PERSISTENCE_ENABLED=true` and a secret-provided `DATABASE_URL`, then run:

```powershell
uv run alembic upgrade head
uv run alembic downgrade -1
```

These migrations create only Super-7-owned tables; do not point them at Apex application tables.
The foundation uses application-generated UUID primary keys and named `CHECK` constraints for stable
lifecycle values. Claim and lease-recovery indexes support future job processing; dispatch and callback
due indexes support future polling. No current runtime path uses these tables.

## Run locally

```powershell
uv run uvicorn main:app --reload
```

Send a JSON request containing backend-owned `videoId`, `playerId`, `videoUrl`, and `callbackUrl`
fields. `videoUrl` must be a safe relative filename within `VIDEO_STORAGE_ROOT` (default:
`/videos`). `POST /analyze` queues that filename for one background analysis worker and returns
HTTP 202; the final result is delivered to `callbackUrl`.

`player.track_id` is a ByteTrack identifier scoped to one analysis request. It is
not a permanent player identity and may differ when the same video is analyzed again.

## Container

The container does not embed ML models. Docker Compose uses a read-only host volume for explicit
model provisioning, so keep model binaries out of Git and the image build context.

1. Create a local deployment environment file:

```powershell
Copy-Item .env.example .env
```

2. Place the configured models in `models/`. The default configuration requires:

```text
models/yolo11n.pt
```

3. Build and start the service:

```powershell
docker compose up --build
```

The service listens on `http://localhost:8000`. The container health check verifies that the
existing OpenAPI document responds at `/openapi.json`; this does not add a health endpoint.

## Layout

```text
src/api/                API, request lifecycle, and public presentation
src/services/           Video-analysis, scoring, and callback services
src/schemas/            API and response contracts
tests/                  Test suite
docs/                   Architecture, remediation, and decision records
```
