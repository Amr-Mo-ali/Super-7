param(
    [ValidateRange(1, 65535)][int]$ApiPort = 8000,
    [switch]$CheckEventLoop
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $repo '.venv\Scripts\python.exe'
$launcher = Join-Path $repo 'scripts\local_durable_uvicorn.py'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'The prepared project virtual environment is unavailable.'
}
if ($CheckEventLoop) {
    & $python $launcher '--check-loop'
    if ($LASTEXITCODE -ne 0) {
        throw 'The local durable event-loop check failed.'
    }
    return
}
$configPath = Join-Path $repo '.local-durable\config.json'
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw 'Persistent local durable setup has not completed.'
}
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
foreach ($path in @($config.video_root, $config.model_path, $config.ball_model_path)) {
    if (-not (Test-Path -LiteralPath $path)) {
        throw 'A configured local video or model path is unavailable.'
    }
}

$values = @{
    PYTHONPATH = (Join-Path $repo 'src')
    SUPER7_DURABLE_LOCAL_ENABLED = '1'
    POSTGRES_HOST = '127.0.0.1'
    POSTGRES_PORT = [string]$config.port
    POSTGRES_DATABASE = [string]$config.database
    POSTGRES_USER = [string]$config.runtime_user
    POSTGRES_PASSWORD = [string]$config.runtime_password
    POSTGRES_CONNECT_TIMEOUT_SECONDS = '2'
    POSTGRES_POOL_MIN_SIZE = '0'
    POSTGRES_POOL_MAX_SIZE = '4'
    POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS = '5'
    POSTGRES_POOL_MAX_WAITERS = '8'
    POSTGRES_POOL_OPEN_TIMEOUT_SECONDS = '10'
    POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS = '10'
    POSTGRES_REQUIRED_SCHEMA_VERSION = 'sprint2_slice4_result_outbox'
    VIDEO_STORAGE_ROOT = [string]$config.video_root
    MODEL_PATH = [string]$config.model_path
    BALL_MODEL_PATH = [string]$config.ball_model_path
    MODEL_DEVICE = 'cpu'
    CALLBACK_TIMEOUT_SECONDS = '10'
    DEBUG_ARTIFACTS_ENABLED = 'false'
    SUPER7_APEX_BEARER_SECRET = [string]$config.apex_bearer_secret
    SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX = [string]$config.idempotency_hmac_secret_hex
    SUPER7_DURABLE_ATTEMPT_LIMIT = '1'
    SUPER7_DURABLE_MODEL_ID = [string]$config.model_id
    SUPER7_DURABLE_RECOVERY_BATCH = '2'
    SUPER7_DURABLE_WORKER_IDLE_SECONDS = '0.1'
    SUPER7_DURABLE_LEASE_SECONDS = '180'
    SUPER7_DURABLE_HEARTBEAT_SECONDS = '20'
    SUPER7_DURABLE_WATCHDOG_SECONDS = '25'
    SUPER7_DURABLE_JITTER_SECONDS = '10'
    SUPER7_DURABLE_FINALIZE_SECONDS = '90'
    SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS = '2'
    SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS = '5'
    SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS = '5000'
    SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS = '1000'
}
$previous = @{}
foreach ($key in $values.Keys) {
    $previous[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
    [Environment]::SetEnvironmentVariable($key, $values[$key], 'Process')
}
try {
    Write-Host "Starting opt-in durable API at http://127.0.0.1:$ApiPort (foreground; Ctrl+C stops it)."
    # Settings currently uses a relative artifact root even when debug output
    # is disabled. Keep transient child sessions outside the Git checkout.
    Push-Location -LiteralPath ([IO.Path]::GetTempPath())
    try {
        & $python $launcher '--port' $ApiPort
        if ($LASTEXITCODE -ne 0) {
            throw 'The local durable API exited unsuccessfully.'
        }
    } finally {
        Pop-Location
    }
} finally {
    foreach ($key in $values.Keys) {
        [Environment]::SetEnvironmentVariable($key, $previous[$key], 'Process')
    }
}
