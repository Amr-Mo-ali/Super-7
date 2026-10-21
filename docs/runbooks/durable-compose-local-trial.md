# Durable Compose local trial: separated roles

This isolated local trial is separate from production `docker-compose.yml`,
`main:app`, the Windows-local `.local-durable/` setup, and VPS deployment.
It uses one API, one worker slot with one spawned child, one dispatcher, and
the existing PostgreSQL volume `super7-durable-compose-pgdata-20261015`.
The request body remains four fields and `videoUrl` remains a filename.
Compose does not enforce a cross-host singleton: the operator must ensure no
second dispatcher or combined runtime uses this database.

Run commands from the actual trial checkout in PowerShell. Keep diagnostics
outside the checkout. Do not print or copy values in `.local-durable-compose/`,
which is Git-ignored and Docker-context-excluded. Never use `down -v`,
`volume rm`, or disposable pytest cleanup on this volume. The callback
validator still requires a public-IP-resolving destination. No staging
destination is approved, so do not submit an analysis yet.

## Fresh preparation and image gate

For a fresh trial only, `prepare` refuses existing credentials, Compose
project, volume, image tag, or occupied `127.0.0.1:18080`; it requires the
pinned PostgreSQL image cached for linux/amd64 and creates no Docker resources.
It writes scoped `api.env`, `worker.env`, and `dispatcher.env` alongside the
existing original and utility files. Stop and inspect any collision or partial
preparation; do not delete or adopt it.

```powershell
Set-Location E:\super7
.\.venv\Scripts\python.exe scripts\durable_compose_setup.py prepare
docker compose --env-file .local-durable-compose\compose.env -f docker-compose.durable-trial.yml config --quiet
docker image inspect 'python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1' --format '{{.Id}}'
docker image inspect 'ghcr.io/astral-sh/uv:0.11.2@sha256:c4f5de312ee66d46810635ffc5df34a1973ba753e7241ce3a08ef979ddd7bea5' --format '{{.Id}}'
```

`config --quiet` avoids printing expanded credentials. The trial Dockerfile
uses pinned dependencies, Linux CPU Torch/torchvision wheels, and headless
OpenCV. Building requires network downloads. The existing Ultralytics
`opencv-python` declaration makes `pip check` report a conflict with the
intentional headless substitution. The production Dockerfile is unchanged.

```powershell
docker build --pull=false --tag football-analysis:durable-compose-trial-local -f Dockerfile.durable-trial .
docker run --rm --network none --entrypoint python football-analysis:durable-compose-trial-local -c "import durable_api_main, durable_worker_main, durable_dispatcher_main, psycopg, torch, torchvision, ultralytics, cv2; print(torch.__version__, torchvision.__version__, ultralytics.__version__, cv2.__version__)"
docker run --rm --network none --entrypoint python football-analysis:durable-compose-trial-local -m pip check
```

All services use this image with `pull_policy: never`. The Dockerfile's HTTP
health check is disabled in Compose for worker and dispatcher, which have no
HTTP listener.

## Bounded credential transition for the existing trial

Do not rerun `prepare`. The earlier utility-file transition must already have
produced `bootstrap-config.json` and `inspect.env`. During an owner-approved
cutover, derive only the three new role files:

```powershell
.\.venv\Scripts\python.exe scripts\durable_compose_setup.py derive-role-files
if ($LASTEXITCODE -ne 0) { throw 'Trial role credential transition refused.' }
docker compose --env-file .local-durable-compose\compose.env -f docker-compose.durable-trial.yml config --quiet
if ($LASTEXITCODE -ne 0) { throw 'Trial Compose validation failed.' }
```

The transition refuses any existing output, missing or inconsistent original
and utility files, or non-ignored output paths. It preserves original
credential bytes. `api.env` receives Apex Bearer and HMAC secrets, runtime
database credentials, and admission settings. `worker.env` receives runtime
database credentials, model/video paths, and worker timing, with no Apex
admission secrets. `dispatcher.env` receives only runtime database credentials
and dispatcher opt-in. PostgreSQL, bootstrap, and inspect retain their
corrected credential scope. Legacy `runtime.env` remains for guarded rollback
and is not attached to a separated service. Do not rerun bootstrap or
recreate PostgreSQL for this transition.

## Fresh trial bootstrap and start

Only on a fresh trial, after image and collision gates pass:

```powershell
$trial = @('--env-file', '.local-durable-compose\compose.env', '-f', 'docker-compose.durable-trial.yml')
docker compose @trial up -d postgres
docker compose @trial ps postgres
docker compose @trial run --rm bootstrap
docker compose @trial up -d --no-deps --scale durable-worker=1 --scale durable-dispatcher=1 durable-worker durable-dispatcher
docker compose @trial up -d --no-deps durable-api
docker compose @trial ps -a
Invoke-RestMethod -Uri 'http://127.0.0.1:18080/health/ready'
curl.exe --silent --show-error --output NUL --write-out '%{http_code}' --header 'Content-Type: application/json' --data '{}' 'http://127.0.0.1:18080/analyze'
```

The final request must return `401`, not acceptance. PostgreSQL has no host
port; API publishes only `127.0.0.1:18080` while Uvicorn listens on its
container interface. Bootstrap creates distinct bootstrap, migration-owner,
and runtime roles, applies only committed Slice 1-4 migrations, and checks
restricted grants. It refuses existing roles/database. On failure, preserve
volume and credentials. API `200` readiness proves only its lifespan and
database/schema probe, not worker, dispatcher, video, CV, or callback delivery.
Headless roles emit `durable_worker_ready` and `durable_dispatcher_ready` after
startup and exit nonzero on actor failure. There is no independent probe for
a stalled actor; monitor exit, error logs, and backlog. Process existence
alone is not proof of actor health.

## Later owner cutover from the combined trial

These are future operator steps, not review actions. Apply and commit the
reviewed manifest and setup code in the actual trial checkout first. The block
below runs as one fail-fast operation from `E:\super7` in PowerShell. It does
not automatically resume callers or undo a partial cutover. Investigate a
failed gate while callers stay paused. It never recreates PostgreSQL, runs
bootstrap/migrations, or removes the named volume.

The current application image tag may still refer to the old combined image.
The block records that running container's immutable image ID and retains it
under a content-derived rollback tag **before** rebuilding the trial tag.
Retagging or rebuilding does not update the currently running combined
container. The reviewed source must be clean so its Git revision can be
embedded in and verified from the new image label. The inspect utility is a
bind-mounted current-source script; the image build supplies its current
dependencies and role entry points. A network-disabled one-shot `--help`
probe verifies the new image ID plus source mount before any durable service
is stopped or recreated.

Docker build and Compose can emit progress on stderr. `Invoke-Progress` shows
that output but uses the native exit code as the success gate. JSON inspection
keeps labels and mounts out of nested `--format` quoting. The record written
beside the checkout contains image IDs and revision only, never credentials.
Before `derive-role-files`, direct Docker label filters find the existing
combined API and PostgreSQL containers and require exact counts; JSON
inspection checks their identities. The new Compose manifest is used only
after the role files exist and `config --quiet` succeeds.
Before running, independently confirm all callers/retry sources are paused
and no other host or Compose project runs a dispatcher for this database;
the prompts require explicit operator acknowledgements. Compose cannot prove
cross-host exclusivity.

```powershell
Set-Location E:\super7
& {
    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    $trial = @('--env-file', '.local-durable-compose\compose.env', '-f', 'docker-compose.durable-trial.yml')
    $appTag = 'football-analysis:durable-compose-trial-local'
    $volumeName = 'super7-durable-compose-pgdata-20261015'
    $project = 'super7-durable-compose-trial-20261015'
    $recordPath = 'E:\super7-separated-compose-cutover-record.json'
    $python = '.\.venv\Scripts\python.exe'

    function Invoke-Text {
        param([string]$File, [string[]]$Arguments)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = & $File @Arguments 2>$null
            $code = $LASTEXITCODE
        } finally { $ErrorActionPreference = $previous }
        if ($code -ne 0) { throw "$File exited with code $code." }
        return ($output -join "`n")
    }

    function Invoke-Progress {
        param([string]$File, [string[]]$Arguments)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $File @Arguments 2>&1 | Out-Host
            $code = $LASTEXITCODE
        } finally { $ErrorActionPreference = $previous }
        if ($code -ne 0) { throw "$File exited with code $code." }
    }

    function Get-DockerObject {
        param([string[]]$Arguments)
        $items = @(Invoke-Text docker $Arguments | ConvertFrom-Json)
        if ($items.Count -ne 1) { throw 'Expected one Docker inspection object.' }
        return $items[0]
    }

    function Get-ComposeIds {
        param([string]$Service)
        $raw = Invoke-Text docker (@('compose') + $trial + @('ps', '--all', '--quiet', $Service))
        return @($raw -split "`r?`n" | Where-Object { $_.Trim() })
    }

    function Get-ProjectServiceIds {
        param([string]$Service)
        $raw = Invoke-Text docker @('container', 'ls', '--all', '--quiet',
            '--filter', "label=com.docker.compose.project=$project",
            '--filter', "label=com.docker.compose.service=$Service")
        return @($raw -split "`r?`n" | Where-Object { $_.Trim() })
    }

    function Assert-EmptyBacklog {
        $raw = Invoke-Text docker (@('compose') + $trial + @(
            'run', '--rm', '--no-deps', 'inspect', 'python',
            '/app/src/durable_compose_setup.py', 'inspect-backlog'))
        $counts = $raw | ConvertFrom-Json
        foreach ($name in @('queued', 'running', 'pending_callbacks')) {
            if ($null -eq $counts.$name -or [int]$counts.$name -ne 0) {
                throw "Cutover blocked: $name is not zero."
            }
        }
        Write-Host 'Backlog gate: all three counts are zero.'
    }

    $revision = (Invoke-Text git @('rev-parse', 'HEAD')).Trim()
    if ($revision -notmatch '^[0-9a-f]{40}$') { throw 'Source revision is invalid.' }
    if ((Invoke-Text git @('status', '--porcelain')).Trim()) {
        throw 'Commit and clean the reviewed source before cutover.'
    }
    if (Test-Path -LiteralPath $recordPath) { throw 'Cutover record already exists; inspect it.' }
    if ((Read-Host 'Confirm all callers and retries are paused: type CALLERS_PAUSED') -cne 'CALLERS_PAUSED') {
        throw 'Callers were not confirmed paused.'
    }
    if ((Read-Host 'Confirm no other dispatcher uses this database: type SINGLE_DISPATCHER') -cne 'SINGLE_DISPATCHER') {
        throw 'Cross-host dispatcher exclusivity was not confirmed.'
    }

    $oldIds = @(Get-ProjectServiceIds 'durable-api')
    if ($oldIds.Count -ne 1) { throw 'Expected exactly one old combined container.' }
    if (@(Get-ProjectServiceIds 'durable-worker').Count -ne 0 -or
        @(Get-ProjectServiceIds 'durable-dispatcher').Count -ne 0) {
        throw 'Separated actors already exist; refusing to overlap runtimes.'
    }
    $old = Get-DockerObject @('container', 'inspect', $oldIds[0])
    if (-not $old.State.Running -or $old.Config.Labels.'com.docker.compose.project' -ne $project -or
        $old.Config.Labels.'com.docker.compose.service' -ne 'durable-api') {
        throw 'The old combined container identity or running state is wrong.'
    }
    $oldImageId = [string]$old.Image
    $currentTagImage = Get-DockerObject @('image', 'inspect', $appTag)
    if ($currentTagImage.Id -ne $oldImageId) { throw 'The trial tag no longer points to the running combined image.' }

    $postgresIds = @(Get-ProjectServiceIds 'postgres')
    if ($postgresIds.Count -ne 1) { throw 'Expected one running trial PostgreSQL container.' }
    $postgres = Get-DockerObject @('container', 'inspect', $postgresIds[0])
    $volume = Get-DockerObject @('volume', 'inspect', $volumeName)
    if (-not $postgres.State.Running -or
        $postgres.Config.Labels.'com.docker.compose.project' -ne $project -or
        $postgres.Config.Labels.'com.docker.compose.service' -ne 'postgres' -or
        $volume.Name -ne $volumeName -or
        -not $volume.Labels.'com.super7.durable-compose-trial.owner' -or
        @($postgres.Mounts | Where-Object {
            $_.Type -eq 'volume' -and $_.Name -eq $volumeName -and
            $_.Destination -eq '/var/lib/postgresql/data'
        }).Count -ne 1) {
        throw 'PostgreSQL identity, owner label, or volume mount differs.'
    }
    Invoke-Progress $python @('scripts\durable_compose_setup.py', 'verify-volume')
    Invoke-Progress $python @('scripts\durable_compose_setup.py', 'derive-role-files')
    Invoke-Progress docker (@('compose') + $trial + @('config', '--quiet'))

    $rollbackTag = 'football-analysis:durable-compose-trial-rollback-' +
        ($oldImageId -replace '^sha256:', '')
    Invoke-Progress docker @('image', 'tag', $oldImageId, $rollbackTag)
    if ((Get-DockerObject @('image', 'inspect', $rollbackTag)).Id -ne $oldImageId) {
        throw 'Rollback image tag does not retain the old image.'
    }

    Invoke-Progress docker @('build', '--pull=false', '--label',
        "org.opencontainers.image.revision=$revision", '--tag', $appTag,
        '-f', 'Dockerfile.durable-trial', '.')
    $newImage = Get-DockerObject @('image', 'inspect', $appTag)
    $newImageId = [string]$newImage.Id
    if ($newImageId -eq $oldImageId -or
        $newImage.Config.Labels.'org.opencontainers.image.revision' -ne $revision) {
        throw 'New image ID or source revision label failed verification.'
    }
    if ((Get-DockerObject @('container', 'inspect', $oldIds[0])).Image -ne $oldImageId) {
        throw 'The running combined container image changed unexpectedly.'
    }
    $scriptSource = (Resolve-Path -LiteralPath 'scripts\durable_compose_setup.py').Path
    $help = Invoke-Text docker @('run', '--rm', '--network', 'none',
        '--mount', "type=bind,source=$scriptSource,target=/app/src/durable_compose_setup.py,readonly",
        '--entrypoint', 'python', $newImageId,
        '/app/src/durable_compose_setup.py', '--help')
    if ($help -notmatch '\binspect-backlog\b') {
        throw 'Verified image and mounted setup script do not expose inspect-backlog.'
    }
    Write-Host "Old image: $oldImageId"
    Write-Host "Rollback tag: $rollbackTag"
    Write-Host "New image: $newImageId"
    Write-Host "Source revision: $revision"

    Assert-EmptyBacklog
    Start-Sleep -Seconds 5
    Assert-EmptyBacklog
    $record = [ordered]@{
        source_revision = $revision; old_image_id = $oldImageId
        rollback_tag = $rollbackTag; new_image_id = $newImageId
        old_container_id = $oldIds[0]; postgres_container_id = $postgresIds[0]
    }
    [System.IO.File]::WriteAllText($recordPath, ($record | ConvertTo-Json),
        [System.Text.UTF8Encoding]::new($false))

    Invoke-Progress docker (@('compose') + $trial + @('stop', '-t', '180', 'durable-api'))
    $stopped = Get-DockerObject @('container', 'inspect', $oldIds[0])
    if ($stopped.State.Running -or $stopped.State.Status -ne 'exited' -or
        [int]$stopped.State.ExitCode -ne 0 -or $stopped.State.OOMKilled) {
        throw 'Old combined container did not exit normally; do not start a dispatcher.'
    }
    Assert-EmptyBacklog
    Invoke-Progress docker (@('compose') + $trial + @(
        'up', '-d', '--no-deps', '--scale', 'durable-worker=1', 'durable-worker'))
    Invoke-Progress docker (@('compose') + $trial + @(
        'up', '-d', '--no-deps', '--scale', 'durable-dispatcher=1', 'durable-dispatcher'))
    Invoke-Progress docker (@('compose') + $trial + @('up', '-d', '--no-deps', 'durable-api'))

    foreach ($service in @('durable-worker', 'durable-dispatcher', 'durable-api')) {
        $ids = @(Get-ComposeIds $service)
        if ($ids.Count -ne 1) { throw "Expected exactly one $service container." }
        $container = Get-DockerObject @('container', 'inspect', $ids[0])
        if (-not $container.State.Running -or $container.Image -ne $newImageId -or
            $container.Config.Labels.'com.docker.compose.service' -ne $service) {
            throw "$service is not running the verified new image."
        }
        if ($service -eq 'durable-worker') {
            foreach ($destination in @('/models', '/videos')) {
                if (@($container.Mounts | Where-Object {
                    $_.Destination -eq $destination -and $_.Type -eq 'bind' -and -not $_.RW
                }).Count -ne 1) { throw "Worker $destination read-only mount is missing." }
            }
        } elseif (@($container.Mounts | Where-Object {
            $_.Destination -in @('/models', '/videos')
        }).Count -ne 0) { throw "$service unexpectedly has model/video mounts." }
    }
    $postgresAfter = Get-DockerObject @('container', 'inspect', $postgresIds[0])
    if (-not $postgresAfter.State.Running -or $postgresAfter.Id -ne $postgres.Id) {
        throw 'The existing PostgreSQL container did not remain running.'
    }
    $deadline = (Get-Date).AddSeconds(90)
    do {
        $workerLogs = Invoke-Text docker (@('compose') + $trial + @(
            'logs', '--no-color', '--tail', '100', 'durable-worker'))
        $dispatcherLogs = Invoke-Text docker (@('compose') + $trial + @(
            'logs', '--no-color', '--tail', '100', 'durable-dispatcher'))
        if ($workerLogs -match 'durable_worker_ready' -and
            $dispatcherLogs -match 'durable_dispatcher_ready') { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($workerLogs -notmatch 'durable_worker_ready' -or
        $dispatcherLogs -notmatch 'durable_dispatcher_ready') {
        throw 'A role-ready event is missing; inspect actor logs and process state.'
    }
    foreach ($service in @('durable-worker', 'durable-dispatcher')) {
        $id = @(Get-ComposeIds $service)[0]
        if (-not (Get-DockerObject @('container', 'inspect', $id)).State.Running) {
            throw "$service exited after its ready event."
        }
    }
    $deadline = (Get-Date).AddSeconds(90)
    $ready = $null
    do {
        try { $ready = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri 'http://127.0.0.1:18080/health/ready' }
        catch { $ready = $null }
        if ($null -ne $ready -and [int]$ready.StatusCode -eq 200) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($null -eq $ready -or [int]$ready.StatusCode -ne 200) { throw 'API readiness is not 200.' }
    Assert-EmptyBacklog
    Write-Host 'Cutover gates passed. Resume callers only under the approved callback policy.'
}
```

The script refuses a pre-existing cutover record, role containers, unexpected
image tag, or nonzero backlog. A failed post-stop check leaves callers paused
and requires investigation; do not run the block again blindly. The worker
and dispatcher have no independent stalled-actor health probe. Continue to
watch exits, role logs, and backlog after callers resume.

## Guarded rollback and restart

Rollback requires callers paused again and zero queued/running jobs and
pending callbacks. If work remains or a native child is still running,
investigate and drain it first. Stop separated API, then dispatcher and
worker, and confirm all have exited. The old manifest alone is insufficient:
its application tag now points to the new image. Retag the recorded old image
ID, verify it, restore the old combined manifest and legacy `runtime.env`,
then start only its `durable-api` against existing PostgreSQL. Never overlap
combined and separated dispatchers. Preserve database data, volume, and all
credential files. Do not rerun bootstrap or migrate. If a gate fails, keep
callers paused and investigate rather than automatically restarting actors.

```powershell
Set-Location E:\super7
& {
    Set-StrictMode -Version Latest
    $ErrorActionPreference = 'Stop'
    $trial = @('--env-file', '.local-durable-compose\compose.env', '-f', 'docker-compose.durable-trial.yml')
    $appTag = 'football-analysis:durable-compose-trial-local'
    $record = Get-Content -LiteralPath 'E:\super7-separated-compose-cutover-record.json' -Raw | ConvertFrom-Json
    if ((Read-Host 'Confirm callers and retries are paused: type CALLERS_PAUSED') -cne 'CALLERS_PAUSED') {
        throw 'Callers were not confirmed paused.'
    }
    function Invoke-Checked {
        param([string]$File, [string[]]$Arguments)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $output = & $File @Arguments 2>&1
            $code = $LASTEXITCODE
        } finally { $ErrorActionPreference = $previous }
        if ($code -ne 0) { throw "$File exited with code $code." }
        return ($output -join "`n")
    }
    function Get-Inspect {
        param([string[]]$Arguments)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $json = & docker @Arguments 2>$null
            $code = $LASTEXITCODE
        } finally { $ErrorActionPreference = $previous }
        if ($code -ne 0) { throw "Docker inspection exited with code $code." }
        $items = @($json | ConvertFrom-Json)
        if ($items.Count -ne 1) { throw 'Expected one Docker inspection object.' }
        return $items[0]
    }
    $current = Get-Inspect @('image', 'inspect', $appTag)
    $old = Get-Inspect @('image', 'inspect', [string]$record.rollback_tag)
    if ($current.Id -ne $record.new_image_id -or $old.Id -ne $record.old_image_id) {
        throw 'Recorded old/new image identities no longer match.'
    }
    $raw = & docker compose @trial run --rm --no-deps inspect python /app/src/durable_compose_setup.py inspect-backlog 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Rollback backlog inspection failed.' }
    $counts = ($raw -join "`n") | ConvertFrom-Json
    foreach ($name in @('queued', 'running', 'pending_callbacks')) {
        if ($null -eq $counts.$name -or [int]$counts.$name -ne 0) {
            throw "Rollback blocked: $name is not zero."
        }
    }
    Invoke-Checked docker (@('compose') + $trial + @('stop', '-t', '180', 'durable-api')) | Out-Host
    Invoke-Checked docker (@('compose') + $trial + @('stop', '-t', '180', 'durable-dispatcher', 'durable-worker')) | Out-Host
    foreach ($service in @('durable-api', 'durable-dispatcher', 'durable-worker')) {
        $ids = @(& docker compose @trial ps --all --quiet $service 2>$null |
            Where-Object { $_.Trim() })
        if ($LASTEXITCODE -ne 0 -or $ids.Count -ne 1) {
            throw "Expected one stopped $service container."
        }
        $container = Get-Inspect @('container', 'inspect', [string]$ids[0])
        if ($container.State.Running -or $container.State.Status -ne 'exited' -or
            [int]$container.State.ExitCode -ne 0) {
            throw "$service did not exit normally."
        }
    }
    Invoke-Checked docker @('image', 'tag', [string]$record.old_image_id, $appTag) | Out-Host
    if ((Get-Inspect @('image', 'inspect', $appTag)).Id -ne $record.old_image_id) {
        throw 'Rollback tag restoration failed.'
    }
    $manifest = & git show 4a6d51dddd87903b05b43500f8cab302e9edda22:docker-compose.durable-trial.yml 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Could not retrieve the reviewed combined manifest.' }
    [System.IO.File]::WriteAllText('docker-compose.durable-trial.yml',
        (($manifest -join "`n") + "`n"), [System.Text.UTF8Encoding]::new($false))
    Invoke-Checked docker (@('compose') + $trial + @('config', '--quiet')) | Out-Host
    Invoke-Checked docker (@('compose') + $trial + @('up', '-d', '--no-deps', 'durable-api')) | Out-Host
    $combinedIds = @(& docker compose @trial ps --all --quiet durable-api 2>$null |
        Where-Object { $_.Trim() })
    if ($LASTEXITCODE -ne 0 -or $combinedIds.Count -ne 1) {
        throw 'Expected one restored combined container.'
    }
    $combined = Get-Inspect @('container', 'inspect', [string]$combinedIds[0])
    if (-not $combined.State.Running -or $combined.Image -ne $record.old_image_id) {
        throw 'The restored combined container is not running the old image.'
    }
    $deadline = (Get-Date).AddSeconds(90)
    $ready = $null
    do {
        try { $ready = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 -Uri 'http://127.0.0.1:18080/health/ready' }
        catch { $ready = $null }
        if ($null -ne $ready -and [int]$ready.StatusCode -eq 200) { break }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    if ($null -eq $ready -or [int]$ready.StatusCode -ne 200) {
        throw 'Restored API readiness is not 200.'
    }
    $networkName = 'super7-durable-compose-trial-20261015_default'
    $network = Get-Inspect @('network', 'inspect', $networkName)
    if ($network.Labels.'com.docker.compose.project' -ne 'super7-durable-compose-trial-20261015') {
        throw 'Trial network identity differs.'
    }
    $scriptSource = (Resolve-Path -LiteralPath 'scripts\durable_compose_setup.py').Path
    $afterRaw = & docker run --rm --network $networkName `
        --env-file '.local-durable-compose\inspect.env' `
        --mount "type=bind,source=$scriptSource,target=/app/src/durable_compose_setup.py,readonly" `
        --entrypoint python $record.new_image_id `
        /app/src/durable_compose_setup.py inspect-backlog 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Post-rollback backlog inspection failed.' }
    $after = ($afterRaw -join "`n") | ConvertFrom-Json
    foreach ($name in @('queued', 'running', 'pending_callbacks')) {
        if ($null -eq $after.$name -or [int]$after.$name -ne 0) {
            throw "Rollback blocked after restore: $name is not zero."
        }
    }
    Write-Host 'Rollback gates passed. Resume callers only under the approved callback policy.'
}
```

Both backlog checks use the verified new image: Compose before retagging and
an ephemeral Docker utility on the unchanged trial network afterward. The
old combined image may lack the new utility command. Do not infer an empty
backlog from readiness or a successful container start.

For ordinary separated restart, pause callers and drain outstanding work
when possible. Stop API, dispatcher, and worker before PostgreSQL. Verify
the volume, start PostgreSQL if needed, then exactly one worker, one
dispatcher, and API. Native worker child shutdown can remain unbounded.

## Request and state after staging callback approval

Use an owner-approved reachable public-IP-resolving callback URL. The body
remains `videoId`, `playerId`, filename-only `videoUrl`, and `callbackUrl`;
headers remain Bearer authorization and `Idempotency-Key`. Reuse the key
only for an exact retry. No callback signing is supplied in this local trial.
The public API has no job-status endpoint. Set `$analysisId` to an accepted
job ID; restricted inspect reports job/attempt status and callback event
state without result JSON or payloads:

```powershell
docker compose @trial run --rm --no-deps inspect python /app/src/durable_compose_setup.py inspect $analysisId
```

Insufficient evidence may legitimately yield null ratings.
