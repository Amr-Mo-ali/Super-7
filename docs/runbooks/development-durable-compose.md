# Development durable roles on the existing PostgreSQL database

This is a reviewed cutover procedure, not an instruction to deploy from this
checkout. Use `docker-compose.development.yml` alone from a verified
Development checkout. It creates one API, four
single-slot workers (`--scale worker=4`), and one dispatcher on the existing
`super7-net` network. It does not create, migrate, or replace PostgreSQL.
The database remains `super7` with runtime user `super7_runtime`, and the
API alone publishes `127.0.0.1:18080`. The configured cap is 300 `QUEUED`
rows **across that database**, enforced by the admission transaction's
PostgreSQL advisory lock and `analysis_jobs` count. This is not 300 per worker
and is not a cap on `RUNNING` jobs. Rejection at 300 was not load-tested.
The existing `ai-dev.4goatsksa.com` proxy route is outside this change;
verify it still targets the loopback API after cutover without changing DNS
or Nginx here.

## Prepare and verify without changing services

The existing manually operated checkout is
`$HOME/super7-candidate-37023abbe80b1b9004ae10a783a64e7d177c6efa`.
Its directory name is not evidence of its current revision. After this
manifest is published there, verify the resolved absolute path and full Git
HEAD against a separately reviewed commit before selecting an image or
stopping a service. Never substitute an assumed `/opt/Super-7` checkout.

On the Development server, prepare a private `compose.env` and three role
files outside the Git checkout, for example under
`/opt/super7-secrets/development/`. Create the directory and empty files
without printing credentials, then fill each file through the private
credential channel:

```bash
sudo install -d -m 0700 -o "$(id -un)" -g "$(id -gn)" /opt/super7-secrets/development
umask 077
touch /opt/super7-secrets/development/{compose,api,worker,dispatcher}.env
chmod 0600 /opt/super7-secrets/development/{compose,api,worker,dispatcher}.env
```

Edit and verify these files before the cutover. Do not paste their contents
into logs or review artifacts. The interpolation file needs only these
non-secret references and verification values:

```dotenv
SUPER7_DEV_IMAGE=football-analysis:dev-REVIEWED-REVISION
SUPER7_DEV_API_ENV=/opt/super7-secrets/development/api.env
SUPER7_DEV_WORKER_ENV=/opt/super7-secrets/development/worker.env
SUPER7_DEV_DISPATCHER_ENV=/opt/super7-secrets/development/dispatcher.env
SUPER7_DEV_REVIEWED_REVISION=FULL-REVIEWED-GIT-COMMIT
SUPER7_DEV_EXPECTED_IMAGE_ID=sha256:REVIEWED-IMAGE-ID
```

Each role file needs its existing `POSTGRES_PASSWORD`. `api.env` alone needs
`SUPER7_APEX_BEARER_SECRET`, `SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX`,
`SUPER7_DURABLE_ATTEMPT_LIMIT`, `SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS`,
and `SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS`. `worker.env` alone needs
`SUPER7_R2_ENDPOINT`, `SUPER7_R2_ACCESS_KEY_ID`,
`SUPER7_R2_SECRET_ACCESS_KEY`, `SUPER7_DURABLE_MODEL_ID`,
`SUPER7_DURABLE_RECOVERY_BATCH`, `SUPER7_DURABLE_WORKER_IDLE_SECONDS`,
`SUPER7_DURABLE_LEASE_SECONDS`, `SUPER7_DURABLE_HEARTBEAT_SECONDS`,
`SUPER7_DURABLE_WATCHDOG_SECONDS`, `SUPER7_DURABLE_JITTER_SECONDS`,
`SUPER7_DURABLE_FINALIZE_SECONDS`,
`SUPER7_DURABLE_FINALIZE_RETRY_ONE_SECONDS`,
`SUPER7_DURABLE_FINALIZE_RETRY_TWO_SECONDS`,
`SUPER7_DURABLE_DB_OPERATION_TIMEOUT_MS`, and
`SUPER7_DURABLE_DB_LOCK_TIMEOUT_MS`. `dispatcher.env` needs only
`POSTGRES_PASSWORD`. Populate these files from the actual Development
configuration through a private channel before stopping any container. Check
file ownership and permissions without displaying values. Keep the current
attempt, lease, recovery, timeout, and HMAC values; a new HMAC key would
break existing idempotency lookups. Never put API credentials in a worker or
dispatcher file, or R2 credentials in an API or dispatcher file. R2 access
must be read-only for the existing `4goats-media-dev` bucket. The bucket is
fixed in worker code; no bucket setting in Compose changes it.

Use a unique, reviewed source-revision image tag or immutable digest built
from the root `Dockerfile`. Record the exact selected image ID and full source
revision in `compose.env`; compare them with the published checkout and
currently running Development containers. Do not infer that
`football-analysis:local` and any Production tag have the same bytes or
dependencies. Confirm the selected image imports the three durable role
modules and R2/CUDA dependencies, and that `/models/yolo11n.pt` is the
reviewed weight for `SUPER7_DURABLE_MODEL_ID`. A GPU reservation alone does
not prove CUDA inference works.

```bash
set -euo pipefail
super7_dev_checkout=$(realpath -- "$HOME/super7-candidate-37023abbe80b1b9004ae10a783a64e7d177c6efa")
. /opt/super7-secrets/development/compose.env
test "$(git -C "$super7_dev_checkout" rev-parse --show-toplevel)" = "$super7_dev_checkout"
test "$(git -C "$super7_dev_checkout" rev-parse HEAD)" = "$SUPER7_DEV_REVIEWED_REVISION"
git -C "$super7_dev_checkout" ls-files --error-unmatch docker-compose.development.yml
git -C "$super7_dev_checkout" status --short
cd "$super7_dev_checkout"
compose=(sudo docker compose --env-file /opt/super7-secrets/development/compose.env -f "$super7_dev_checkout/docker-compose.development.yml")
sudo docker inspect --format '{{.Name}} {{.Image}} {{with .Config.Labels}}{{index . "com.docker.compose.project"}}{{end}}' super7-api super7-worker super7-worker-2 super7-worker-3 super7-worker-4 super7-dispatcher
image_id=$(sudo docker image inspect "$SUPER7_DEV_IMAGE" --format '{{.Id}}')
test "$image_id" = "$SUPER7_DEV_EXPECTED_IMAGE_ID"
test "$(sudo docker image inspect "$SUPER7_DEV_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" = "$SUPER7_DEV_REVIEWED_REVISION"
sudo docker run --rm --network none --gpus all --mount "type=bind,source=$super7_dev_checkout/models,target=/models,readonly" --entrypoint python "$SUPER7_DEV_IMAGE" -c 'import durable_api_main, durable_worker_main, durable_dispatcher_main, psycopg, boto3, torch, ultralytics; assert torch.cuda.is_available(); ultralytics.YOLO("/models/yolo11n.pt")'
"${compose[@]}" config --quiet
```

The reviewed commit must be the one actually published in this checkout, and
the image revision label must match it. Record old container image IDs
privately for rollback; do not assume they equal the new image ID.
If the old containers have Compose labels, identify their actual owning
project. If they belong to `super7-development`, stop and review how Compose
would manage them before proceeding. `config --quiet` avoids
printing expanded secrets. Check `sudo docker compose config` only in a private
session; it expands environment files. Confirm the external network and
existing PostgreSQL container, database, schema revision, and data before
starting roles. Do not use the isolated trial bootstrap or migrations.

## Owner-approved cutover

Pause new Development submissions and caller retries first. Inspect
`analysis_jobs` until both `QUEUED` and `RUNNING` are zero, and
`analysis_callback_outbox` until `PENDING` and `RETRYING` are zero. Record
the row counts and a few non-sensitive existing job/result identifiers for
comparison after cutover. If any work remains, keep the old fleet running
and wait or investigate. Keep Production services stopped throughout this
Development load experiment; both environments share the GPU and cannot
independently start full fleets without a resource allocation decision.

For the drain and preservation check, use the existing PostgreSQL container's
local administrative identity if its authentication policy permits it. The
following read-only query gives the required counts without exposing rows:

```bash
sudo docker exec -u postgres super7-postgres psql -d super7 -Atqc "SELECT current_database(); SELECT state, count(*) FROM analysis_jobs GROUP BY state ORDER BY state; SELECT delivery_state, count(*) FROM analysis_callback_outbox GROUP BY delivery_state ORDER BY delivery_state; SELECT migration_id FROM _yoyo_migration ORDER BY migration_id;"
```

If local PostgreSQL authentication differs, use an approved read-only
connection and the same SQL. The applied migration set must include
`sprint2_slice5_video_integrity`. Repeat the count query after cutover and
rollback. Do not infer preservation from a zero backlog alone; compare
previously recorded, non-sensitive job and result identifiers too.

After the drain and image/configuration gates pass, use these commands from
the Development checkout:

```bash
set -euo pipefail
super7_dev_checkout=$(realpath -- "$HOME/super7-candidate-37023abbe80b1b9004ae10a783a64e7d177c6efa")
. /opt/super7-secrets/development/compose.env
test "$(git -C "$super7_dev_checkout" rev-parse HEAD)" = "$SUPER7_DEV_REVIEWED_REVISION"
test "$(sudo docker image inspect "$SUPER7_DEV_IMAGE" --format '{{.Id}}')" = "$SUPER7_DEV_EXPECTED_IMAGE_ID"
cd "$super7_dev_checkout"
compose=(sudo docker compose --env-file /opt/super7-secrets/development/compose.env -f "$super7_dev_checkout/docker-compose.development.yml")
"${compose[@]}" config --quiet
test -z "$(sudo docker ps --all --filter label=com.docker.compose.project=super7-development --format '{{.Names}}')"
sudo docker stop --timeout 30 super7-api
sudo docker stop --timeout 30 super7-dispatcher
sudo docker stop --timeout 180 super7-worker super7-worker-2 super7-worker-3 super7-worker-4
for old_role in super7-api super7-dispatcher super7-worker super7-worker-2 super7-worker-3 super7-worker-4; do
  test "$(sudo docker inspect --format '{{.State.Status}}' "$old_role")" = exited
  test "$(sudo docker inspect --format '{{.State.ExitCode}}' "$old_role")" = 0
  test "$(sudo docker inspect --format '{{.State.OOMKilled}}' "$old_role")" = false
done
"${compose[@]}" up -d --no-deps --scale worker=4 api worker dispatcher
"${compose[@]}" ps --all
curl --fail --silent http://127.0.0.1:18080/health/ready
```

Stop before `up` if an old role is still running, has a nonzero exit or OOM
status, or an old worker has not settled its child. The guards above halt the
script on abnormal container state. `sudo docker stop --timeout` can force termination;
the native worker child may outlive its grace period. Starting a new fleet
with a still-running old worker or dispatcher is unsafe. The dispatcher has
database ownership fencing, but a second one must not be used as a cutover
strategy. Do not use `down`, `rm`, `down -v`, or recreate PostgreSQL.

Verify that Compose reports exactly one API, four workers, and one
dispatcher, all on the recorded image ID. Check each worker's
`durable_worker_ready` event, the dispatcher's
`durable_dispatcher_ready` event, and continued process state; they have no
HTTP listener or HTTP healthcheck. Query from the API container with its
existing runtime credential and verify `current_database() = super7` and
`current_user = super7_runtime`:

```bash
"${compose[@]}" exec -T api python -c 'import os, psycopg; c = psycopg.connect(host=os.environ["POSTGRES_HOST"], port=os.environ["POSTGRES_PORT"], dbname=os.environ["POSTGRES_DATABASE"], user=os.environ["POSTGRES_USER"], password=os.environ["POSTGRES_PASSWORD"]); print(c.execute("SELECT current_database(), current_user").fetchone()); c.close()'
```

Compare the recorded job/result rows and backlog counts; API readiness alone does
not prove they were preserved. Confirm only the API has the loopback port,
only workers have GPU and read-only `/models`, and no service publishes a
database port. Resume submissions only after these gates pass.

## Rollback without deleting data

Pause Development submissions again. Drain or account for all queued/running
jobs and pending callbacks. A failed role may leave a lease pending; wait for
recovery and inspect the database instead of assuming an empty backlog.
Verify the six retained old containers and their recorded image IDs before
restarting them. Then:

```bash
set -euo pipefail
super7_dev_checkout=$(realpath -- "$HOME/super7-candidate-37023abbe80b1b9004ae10a783a64e7d177c6efa")
. /opt/super7-secrets/development/compose.env
test "$(git -C "$super7_dev_checkout" rev-parse HEAD)" = "$SUPER7_DEV_REVIEWED_REVISION"
cd "$super7_dev_checkout"
compose=(sudo docker compose --env-file /opt/super7-secrets/development/compose.env -f "$super7_dev_checkout/docker-compose.development.yml")
"${compose[@]}" stop --timeout 180 api dispatcher worker
"${compose[@]}" ps --all
test -z "$(sudo docker ps --filter label=com.docker.compose.project=super7-development --format '{{.Names}}')"
sudo docker start super7-worker super7-worker-2 super7-worker-3 super7-worker-4
sudo docker start super7-dispatcher
sudo docker start super7-api
sudo docker inspect --format '{{.Name}} {{.State.Status}} {{.Image}}' super7-api super7-dispatcher super7-worker super7-worker-2 super7-worker-3 super7-worker-4
curl --fail --silent http://127.0.0.1:18080/health/ready
```

Before `sudo docker start`, confirm every Compose worker and dispatcher has exited;
if one has not, keep callers paused and investigate. Verify the old fleet's
database identity, readiness, job/result rows, callback backlog, and image
IDs before resuming callers. No rollback step deletes database data, images,
or containers. Keep diagnostics outside the production Git checkout.

The four-worker evidence is bounded: four workers processed jobs
concurrently, GPU utilization reached about 90–99%, a repeated eight-job
batch completed callbacks within 235 seconds, worker RAM totaled about
5.24 GiB afterward, GPU memory was 1312 MiB, and no R2 staging directories
remained. This does not prove maximum capacity, leak freedom, or rejection
behavior at 300 queued jobs.
