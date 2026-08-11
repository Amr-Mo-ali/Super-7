# CI/CD deployment

## Architecture

```text
Developer
   |
git push main
   |
GitHub CI
   |
quality checks + tests + Docker build
   |
CI success
   |
Deploy workflow
   |
SSH into VPS
   |
git pull --ff-only
   |
docker compose build
   |
docker compose up -d
   |
health check
```

`.github/workflows/ci.yml` is the single CI workflow. It performs validation only:
formatting, linting, type checking, tests, and a Docker image build.

`.github/workflows/deploy.yml` runs only after a successful `CI` workflow run on
`main`. It deploys to the GitHub `production` environment and contains no CI
checks.

## Required GitHub configuration

Configure these repository secrets; never commit their values:

- `VPS_HOST`
- `VPS_USER`
- `VPS_SSH_KEY`

The deploy workflow uses the existing GitHub environment named `production`.

Two separate SSH key directions are required:

- **VPS -> GitHub:** the VPS deploy key lets the server pull repository changes.
- **GitHub Actions -> VPS:** `VPS_SSH_KEY` lets the workflow SSH into production.

These keys must be independent; do not reuse either private key in the other
direction.

## Production safety rules

Before changing source, deployment requires a clean Git working tree and the
`main` branch. It uses `git pull --ff-only origin main`; it never force-resets,
runs `git clean`, or force-checks out files. It validates Compose before building
and runs `docker compose up -d`, never `docker compose down`.

The deployment does not delete or overwrite production `.env`, models, videos,
or data. Existing mounts, including
`/var/www/apex-backend/uploads/videos:/videos:ro`, remain managed by the
production Compose configuration. Deployment succeeds only after the local
health endpoint returns HTTP 2xx.

## Manual verification

On the VPS:

```bash
cd /opt/Super-7
git status
git branch --show-current
git pull --ff-only origin main
docker compose config --quiet
docker compose ps
curl --fail http://127.0.0.1:8000/health/ready
```

Verify the public API separately:

```bash
curl --fail https://agoksa.com/openapi.json
```

## Failure behavior

- **CI fails:** Deploy is not started.
- **SSH fails:** the Deploy workflow fails before it can change the VPS.
- **Git tree is dirty:** deployment prints `git status --short` and exits without
  resetting or deleting files.
- **Docker validation, build, or startup fails:** the workflow fails and prints
  Compose status plus the most recent 100 log lines.
- **Health check fails:** deployment retries up to 20 times, about three seconds
  apart, then prints Compose status and recent logs and fails.

## Deploying and inspecting a deployment

A developer deploys by pushing to `main`:

```bash
git add .
git commit -m "<message>"
git push origin main
```

Inspect execution in GitHub under **Actions -> CI** and **Actions -> Deploy**.
On the VPS, use:

```bash
docker compose ps
docker logs super-7-football-analysis-1 --tail 100
```

#   d e p l o y   t e s t 
Deployment pipeline verification trigger: 2026-08-11
 
 
Deployment pipeline verified through CI/CD.
