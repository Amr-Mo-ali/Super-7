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

## Run locally

```powershell
$env:SUPER7_LEGACY_IN_MEMORY_ENABLED = '1'
uv run uvicorn main:app --reload
```

The in-memory route is for explicit development and tests only. Without this
flag, `main:app` has no `/analyze` route and fails at startup. The combined
durable factory and the separated API, worker, and dispatcher have their own
explicit opt-in settings; see the durable role runbooks.

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

3. The root image and root Compose file have no default runtime role. Building
   the image is supported, but starting it without an explicit reviewed role
   command exits with an error. The root Compose file is not a complete durable
   production deployment. The manual deploy workflow stops before SSH until
   a production API, worker, and dispatcher topology has been reviewed.

   The separate local trial is documented in
   [the durable Compose runbook](docs/runbooks/durable-compose-local-trial.md).

   For a local image build only:

```powershell
docker compose build
```

The integration Compose file explicitly enables the legacy test runtime. It is
not a production deployment. When an API role is deliberately launched, its
health route is `/health/ready`.

## Layout

```text
src/api/                API, request lifecycle, and public presentation
src/services/           Video-analysis, scoring, and callback services
src/schemas/            API and response contracts
tests/                  Test suite
docs/                   Architecture, remediation, and decision records
```
