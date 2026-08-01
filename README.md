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
uv run uvicorn main:app --reload
```

Send a multipart request containing `video`, `player_id`, and optionally `x`, `y`, `width`, `height`, and `frame_index`.

## Container

The project is a single modular monolith, so Docker Compose is not needed. Build the skeleton image with:

```powershell
docker build -t football-analysis .
```

## Layout

```text
src/football_analysis/  Application package (intentionally empty of feature code)
tests/                  Test package
```
