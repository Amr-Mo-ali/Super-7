# Durable Compose local trial (separate from Windows-local setup)

This is an isolated **local** trial of `durable_local_main:create_app`, not
`docker-compose.yml`, `main:app`, the Windows-local `.local-durable/` database,
or a VPS deployment. It composes one API process with one worker slot, one
spawned analysis child at a time, and one callback dispatcher. The trial
database lives in `super7-durable-compose-pgdata-20261015`. Never use
`down -v`, `volume rm`, or the disposable pytest cleanup flow on it.

Run commands from `E:\super7` in PowerShell. The files under ignored
`.local-durable-compose/` contain private credentials. Do not print, copy to
an image, commit, or paste them into reports. Do not reuse `.env` or
`.local-durable/`. The existing callback validator still requires a destination
that resolves only to public IP addresses; no staging destination is currently
approved, so **do not submit an analysis yet**.

## One-time preparation and image gate

```powershell
Set-Location E:\super7
.\.venv\Scripts\python.exe scripts\durable_compose_setup.py prepare
docker compose --env-file .local-durable-compose\compose.env -f docker-compose.durable-trial.yml config --quiet
docker image inspect 'python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1' --format '{{.Id}}'
docker image inspect 'ghcr.io/astral-sh/uv:0.11.2@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5' --format '{{.Id}}'
```

`prepare` refuses an existing credential directory, trial Compose project,
trial volume, or occupied `127.0.0.1:18080`; it also requires the approved
digest-pinned PostgreSQL image already cached for linux/amd64. It creates no
Docker resources. Stop and inspect a collision or partial preparation; never
delete/adopt it or rerun `prepare` over it. `config --quiet` validates Compose
without printing expanded environment values. The image-inspection command
must succeed before building; do not silently pull a base image.

For an **already prepared** trial, do not rerun `prepare`. Before using the
corrected Compose manifest, derive the two service-specific utility inputs
from the existing configuration, then validate without printing resolved
secrets:

```powershell
Set-Location E:\super7
.\.venv\Scripts\python.exe scripts\durable_compose_setup.py derive-service-files
if ($LASTEXITCODE -ne 0) { throw 'Trial credential transition refused; inspect the safe error.' }
docker compose --env-file .local-durable-compose\compose.env -f docker-compose.durable-trial.yml config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Trial Compose validation failed.' }
```

This command refuses either existing output file, a missing or inconsistent
source preparation, or non-ignored output paths. It never rotates or rewrites
the existing `config.json`, `postgres-password.secret`, `compose.env`, or
`runtime.env`. The new ignored `bootstrap-config.json` contains only database,
role, and trial-identity inputs needed by bootstrap; `inspect.env` contains
only the runtime database name and credentials. PostgreSQL continues to use
only its bootstrap username and password file. Only durable-api loads the
full runtime environment, including API authentication/idempotency secrets.
Do not rerun bootstrap or recreate services merely to perform this transition;
preserve the existing database and volume.

The trial-only Dockerfile exports pinned production dependencies from
`uv.lock`, excluding the CUDA-bearing PyPI Torch wheel, torchvision and
GUI OpenCV. It installs the matching official Linux CPU wheels for Torch
2.13.0 and torchvision 0.28.0, and retains locked headless OpenCV. It does
not alter the production Dockerfile, image tag or command. The dependency
downloads require network access; this is not an offline build. The pinned
`uv` source image must be cached or explicitly downloaded before building:

```powershell
docker build --pull=false --tag football-analysis:durable-compose-trial-local -f Dockerfile.durable-trial .
docker run --rm --network none --entrypoint python football-analysis:durable-compose-trial-local -c "import durable_local_main, psycopg, torch, torchvision, ultralytics, cv2; print(torch.__version__, torchvision.__version__, ultralytics.__version__, cv2.__version__)"
docker run --rm --network none --entrypoint python football-analysis:durable-compose-trial-local -m pip check
```

The image imports use the locked versions and CPU Torch/torchvision artifacts.
`pip check` currently reports that Ultralytics declares `opencv-python`
missing: the production Dockerfile also uninstalls GUI OpenCV in favor of
headless OpenCV. Do not conceal this packaging conflict or install both
distributions over the same `cv2` files merely to silence the check.

Confirm the built image exists before any `up`; the trial Compose services
set `pull_policy: never` and will not fetch an image themselves.

## Bootstrap once, then start

Only after the image and collision gates pass:

```powershell
$trial = @('--env-file', '.local-durable-compose\compose.env', '-f', 'docker-compose.durable-trial.yml')
docker compose @trial up -d postgres
docker compose @trial ps postgres
docker compose @trial run --rm bootstrap
docker compose @trial up -d durable-api
docker compose @trial ps
Invoke-RestMethod -Uri 'http://127.0.0.1:18080/health/ready'
curl.exe --silent --show-error --output NUL --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{}' 'http://127.0.0.1:18080/analyze'
```

The final request must return `401`, not acceptance. PostgreSQL has **no host
port**; the API publishes only `127.0.0.1:18080`. The one-time bootstrap
creates distinct bootstrap, migration-owner, and runtime roles, applies only
the committed Slice 1–4 migrations, and verifies the restricted grants. It
refuses existing trial roles/database. If any step fails, preserve the volume
and credentials for inspection; do not repeat bootstrap over partial state.
The application receives only runtime credentials, not the bootstrap or
migration credentials. A `200` readiness response confirms this composition's
DB, worker, dispatcher and read-only video root checks; it is not a CV or
callback-delivery result.

## Request and state, only after staging callback approval

Use an owner-approved, reachable public-IP-resolving staging callback URL.
No callback signing is supplied in this local trial. Keep the same
`Idempotency-Key` for an exact retry; use a new one for an intentionally new
job. The body stays four fields:

```powershell
$cfg = Get-Content -LiteralPath .local-durable-compose\config.json -Raw | ConvertFrom-Json
$key = [guid]::NewGuid().ToString('N')
$headers = @{ Authorization = "Bearer $($cfg.apex_bearer_secret)"; 'Idempotency-Key' = $key }
$body = @{ videoId = 'trial_video_011'; playerId = 'trial_player'; videoUrl = 'video_011.mp4'; callbackUrl = '<approved-staging-https-url>' } | ConvertTo-Json -Compress
$accepted = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:18080/analyze' -Headers $headers -ContentType 'application/json' -Body $body
$accepted.analysisId
```

Keep the token, request body and callback URL out of reports. There is no
public job-status endpoint. Inspect the accepted ID using the restricted
runtime role, without printing result JSON or callback payloads:

```powershell
docker compose @trial run --rm --no-deps inspect python /app/src/durable_compose_setup.py inspect $accepted.analysisId
```

Insufficient evidence may legitimately yield null ratings; do not promise
populated scores.

## Graceful stop and restart

Stop the app first and allow submitted child work to settle. Native inference
shutdown may remain unbounded; this is not a hard-stop guarantee.

```powershell
docker compose @trial stop -t 180 durable-api
docker compose @trial ps
.\.venv\Scripts\python.exe scripts\durable_compose_setup.py verify-volume
docker compose @trial up -d durable-api
Invoke-RestMethod -Uri 'http://127.0.0.1:18080/health/ready'
```

Repeat the restricted `inspect` command with the same job ID and compare its
persisted job, attempt, result and event identity/state. For a full shutdown,
stop `durable-api` before `postgres` with `docker compose @trial stop`; leave
the named volume and ignored credentials intact. Restart with `up -d postgres`
then `up -d durable-api`. Never rerun one-time bootstrap on restart, and never
use `docker compose down -v` for ordinary shutdown.
