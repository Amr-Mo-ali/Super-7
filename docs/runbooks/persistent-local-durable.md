# Persistent local durable run (one worker)

This is an opt-in Windows-local experiment, not the default `main:app` or a
production rollout. Run commands from `E:\super7` in PowerShell. PostgreSQL is
stored in the named Docker volume `super7-durable-local-pgdata-20261015`; **do
not remove that volume when stopping or restarting**. Setup refuses existing
resource names and never adopts or deletes them. It requires the already-local
digest-pinned PostgreSQL 16.15 image; there is no pull or installation step.

## One-time setup

```powershell
Set-Location E:\super7
.\.venv\Scripts\python.exe scripts\local_durable_setup.py init
```

The setup command checks the pinned image and resource-name/port collisions,
creates a loopback-only container and persistent volume, creates separate
bootstrap/migration/runtime credentials, applies the existing Slice 1–4
migrations once, and verifies the restricted runtime grants. It does **not**
start the API or submit analysis. No migration runs during application startup.
The container is `super7-durable-local-pg-20261015`, published only at
`127.0.0.1:55432`. Credentials are generated in Git-ignored
`.local-durable\` and must be kept private; existing `.env` is untouched.
If setup fails after creating a resource, stop and inspect it—do not rerun
`init`, remove the volume, or reuse an existing credential file.

## Start and manual request

Keep the database running. In a dedicated terminal, launch the API in the
foreground (the script loads the generated runtime credential and local-only
settings, then restores prior process environment values on exit):

```powershell
Set-Location E:\super7
.\.venv\Scripts\python.exe scripts\local_durable_setup.py db-start
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\super7\scripts\local_durable_start.ps1 -CheckEventLoop
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\super7\scripts\local_durable_start.ps1
```

The API address is `http://127.0.0.1:8000`; the ASGI factory is
`durable_local_main:create_app`. The foreground launcher runs Uvicorn with
`factory=True` on a Windows Selector event loop **before** the application
lifespan opens Psycopg. `-CheckEventLoop` prints only the actual running loop
type and does not read credentials, start a listener, or connect to PostgreSQL.
The execution-policy option applies only to that foreground PowerShell process;
it does not change the machine policy.
The application composes exactly one worker slot,
one analysis child at a time, and one dispatcher. The run uses CPU models at
`E:\super7\models\yolo11n.pt` for both model paths, mapping their
`<model_path>+<ball_model_path>+bytetrack` catalog key to
`local_yolo11n_pair_bytetrack_v1`. The 180-second lease, 20-second heartbeat,
single attempt, and other settings in the start script are local-experiment
settings—not production defaults. Debug artifacts are disabled; the launcher
uses the OS temporary directory as its working directory so transient child
artifact sessions never sit in the Git checkout.

`video_011.mp4` is available under the configured input directory:
24,863,230 bytes, SHA-256
`ba59a7232e77c801b6a1edc241be170addb92ba5ff3b10a7858799c94530dc87`.
The video filename is not an analysis request until you submit it. No callback
destination has been supplied. Choose a reachable **public-IP-resolving**
HTTP(S) receiver before submitting; private/loopback URLs are rejected by the
unchanged callback validator. Do not use a test-only loopback bypass. The
dispatcher can retry a failing receiver indefinitely, so do not submit to a
placeholder or unverified destination.

After choosing an actual permitted callback URL, submit in a second terminal:

```powershell
Set-Location E:\super7
$cfg = Get-Content -LiteralPath .local-durable\config.json -Raw | ConvertFrom-Json
$key = [guid]::NewGuid().ToString('N')
$headers = @{ Authorization = "Bearer $($cfg.apex_bearer_secret)"; 'Idempotency-Key' = $key }
$body = @{ videoId = 'local_video_011'; playerId = 'local_player'; videoUrl = 'video_011.mp4'; callbackUrl = '<approved-public-https-url>' } | ConvertTo-Json -Compress
$accepted = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/analyze' -Headers $headers -ContentType 'application/json' -Body $body
$accepted.analysisId
```

Keep the same `Idempotency-Key` for retries of that exact request; generate a
new key only for an intentionally new analysis. The four-field body is
unchanged. The bearer credential maps server-side to caller scope `apex`.
Do not paste the credential, request body, or callback URL into logs/reports.
The public API has no job-status endpoint.

## Inspect, stop, and restart without data loss

Use the accepted `analysisId` with the restricted runtime role. The inspection
command reports job/attempt status, durable result presence/version and
duration, callback event ID/state/due time, but not payloads, URLs or secrets:

```powershell
.\.venv\Scripts\python.exe scripts\local_durable_setup.py inspect $accepted.analysisId
```

Stop the API with Ctrl+C in its foreground terminal and allow its shutdown to
finish; native child shutdown can remain unbounded. Then, if desired, stop
only the verified database container (the volume remains):

```powershell
.\.venv\Scripts\python.exe scripts\local_durable_setup.py db-stop
```

To restart, use `db-start` followed by `local_durable_start.ps1` above in a
foreground terminal. Repeat `inspect` with the **same** `analysisId` and
compare the job ID, attempt/result state, callback event ID and delivery state
to the prior output. Restart resumes persisted work and due callback delivery;
it does not create a new request. An unavailable receiver may remain RETRYING.
Insufficient video evidence may legitimately produce null ratings; do not
interpret null as zero or promise populated scores.

Never use `docker rm -v`, `docker volume rm`, `docker compose down -v`, or the
disposable pytest cleanup procedure for this persistent database. This local
setup does not approve VPS deployment, callback signing, API cutover, or
production capacity.
