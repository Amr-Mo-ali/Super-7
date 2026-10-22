# Separated durable server preparation (R2, one GPU worker)

Use only `docker-compose.server.yml`. Root `docker-compose.yml` retains a
disabled historical Apex `/videos` mount; `docker-compose.durable-trial.yml`
is a local CPU trial. Neither is a server rollout command. This runbook does
not authorize public admission or callbacks. The manual deploy workflow still
stops before SSH.

## Exact revision and private inputs

The published baseline `37023abbe80b1b9004ae10a783a64e7d177c6efa`
does **not** contain this configuration. Before using these commands, publish
and review this change, then set the exact resulting 40-character SHA. Do not
substitute the baseline SHA or the moving branch head.

```bash
set -euo pipefail
sha='<PUBLISHED_SERVER_CONFIGURATION_SHA>'
published_ref='<PUBLISHED_GIT_REF>'
[[ "$sha" =~ ^[0-9a-f]{40}$ ]]
test -n "$published_ref"
git clone --no-checkout https://github.com/Amr-Mo-ali/Super-7.git "$HOME/super7-server-$sha"
cd "$HOME/super7-server-$sha"
git fetch --no-tags origin "$published_ref"
test "$(git rev-parse FETCH_HEAD)" = "$sha"
git checkout --detach FETCH_HEAD
test "$(git rev-parse HEAD)" = "$sha"
test -z "$(git status --porcelain)"
```

Set `SUPER7_IMAGE_TAG` to the **actual published SHA containing this server
configuration**, not the baseline above when this work is still uncommitted.
Keep all populated files and model weights outside the Git checkout:

```bash
export SUPER7_SERVER_SECRETS_DIR=/opt/super7-secrets
export SUPER7_SERVER_MODELS_DIR=/opt/super7-models
export SUPER7_IMAGE_TAG="$(git rev-parse HEAD)"
export SUPER7_POSTGRES_VOLUME=super7-server-pgdata
sudo install -d -m 0700 -o "$(id -u)" -g "$(id -g)" \
  "$SUPER7_SERVER_SECRETS_DIR"
sudo install -d -m 0755 "$SUPER7_SERVER_MODELS_DIR"
for role in db-admin api worker dispatcher; do
  test ! -e "$SUPER7_SERVER_SECRETS_DIR/$role.env"
  install -m 0600 "deploy/server-examples/$role.env.example" \
    "$SUPER7_SERVER_SECRETS_DIR/$role.env"
done
test ! -e "$SUPER7_SERVER_SECRETS_DIR/postgres-password"
install -m 0600 /dev/null "$SUPER7_SERVER_SECRETS_DIR/postgres-password"
```

The copy block is for a new secret directory and refuses to overwrite an
existing file. For an existing deployment, inventory and reuse its private
files through a separately reviewed credential transition. An administrator
must populate the private files before Compose startup.
`postgres-password` is the PostgreSQL administrator password. The same four
role passwords must match between `db-admin.env` and the respective role env
files. `api.env` also needs the Apex bearer secret and a hexadecimal HMAC
secret; `worker.env` needs the approved model ID, account-scoped HTTPS R2
endpoint, and dedicated read-only R2 key pair. Do not place an R2 key in the
API or dispatcher env. Do not paste secret values or resolved Compose config
into tickets or logs. Model weights must be readable by container UID 10001
and present at `/models/yolo11n.pt`; both actual model settings point there.
`MODEL_DEVICE=cuda:0` is set in Compose for the worker only.

After populating the password file, make it readable by the non-root
`db-admin` image (UID 10001). Compose file-backed secrets retain host file
ownership and permissions on Linux; the initial operator-owned `0600` file
cannot be read by that role. Keep the other private files operator-owned.

```bash
sudo chown 10001:10001 "$SUPER7_SERVER_SECRETS_DIR/postgres-password"
sudo chmod 0400 "$SUPER7_SERVER_SECRETS_DIR/postgres-password"
test "$(sudo stat -c '%u:%g:%a' \
  "$SUPER7_SERVER_SECRETS_DIR/postgres-password")" = '10001:10001:400'
```

The R2 bucket `4goats-media-dev` and `videos/` prefix are fixed in the
current code; they are not environment settings. Confirm that namespace and
the backend integrity policy before admitting requests. The worker stages at
`/staging` on a private 1 GiB tmpfs, with a 100 MiB object bound and 30 s
transfer deadline in the example. Each staged directory is removed after
its owned transfer and child settle; a hard kill discards the container tmpfs.
The `/scratch` tmpfs is the worker and child working directory; the existing
artifact manager creates request sessions there even with debug rendering
disabled. It also holds the model-library config/cache output.
Avoid changing these limits without a measured video-size and disk budget.
The three worker tmpfs mounts can consume up to 1,792 MiB combined, and
tmpfs pages count toward the worker's memory limit. The 100 MiB object cap
fits within the 1 GiB staging mount, but the 512 MiB scratch and 256 MiB
temporary mounts have not been validated against real model inference.
No worker memory limit or host RAM amount can be inferred from the A4000's
16 GB of GPU memory. Before Compose validation or worker startup, inspect
`free -h`, `docker info --format '{{.MemTotal}}'`, and memory used by other
services on the target; measure peak worker RAM and scratch use with a
representative, authorized video. Set `SUPER7_WORKER_MEMORY_LIMIT` to a
reviewed Docker byte value that leaves headroom beyond all three tmpfs
limits and the measured process/model peak, while preserving capacity for
PostgreSQL, the API, the host, and other workloads. Compose requires this
value and sets memory plus swap to the same limit. Do not start the worker
until that target-specific capacity review is complete.

```bash
free -h
docker info --format '{{.MemTotal}}'
read -r -p 'Reviewed worker memory limit: ' SUPER7_WORKER_MEMORY_LIMIT
export SUPER7_WORKER_MEMORY_LIMIT
test -n "$SUPER7_WORKER_MEMORY_LIMIT"
```

## Build and inspect without starting admission

```bash
docker compose -f docker-compose.server.yml config --quiet
docker compose -f docker-compose.server.yml --profile maintenance build api db-admin
docker compose -f docker-compose.server.yml --profile maintenance run --rm \
  --no-deps db-admin python -c \
  'from pathlib import Path; assert Path("/run/secrets/postgres_password").read_bytes().strip()'
docker compose -f docker-compose.server.yml build worker
docker run --rm --gpus all --network none --entrypoint python \
  "super7-server-gpu-worker:$SUPER7_IMAGE_TAG" \
  -c 'import torch; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'
```

The worker image uses official PyTorch 2.13.0 CUDA 12.6 and torchvision
0.28.0 CUDA 12.6 wheels with exact versions. Their separately reviewed hash
manifest is still missing; do not treat version pins or this device probe as
wheel attestation or actual inference. The GPU probe must run on the target;
local Windows Compose validation cannot establish A4000 behavior.
The image deliberately installs locked headless OpenCV in place of
Ultralytics' declared GUI `opencv-python` dependency. Imports passed locally,
but `uv pip check` still reports that metadata requirement as unsatisfied;
real inference and codec behavior remain target-server checks.

## Database: choose exactly one provisioning path

The PostgreSQL 16.15 image uses an **explicit external** persistent volume
and has no host port. Compose will not silently create or remove the volume.
For a fresh database only, confirm the chosen name is unused, then create it
explicitly with `docker volume create "$SUPER7_POSTGRES_VOLUME"`. For an
existing database, inspect the intended volume and its backup/identity with
the DBA; do not create a replacement. An existing cluster initialized with a
different administrator role/password needs a separately reviewed adoption
procedure. The administrator role is for provisioning only. `db-admin` is a
one-shot maintenance profile; API, worker, and dispatcher use separate login
roles. Neither runtime image nor runtime startup migrates schema.

**New, empty database only:** inspect the intended volume and resource names,
then start PostgreSQL and provision. `provision-fresh` refuses an existing
database or any preexisting role name. It does not migrate. If a partial
provision fails, inspect it with a DBA; do not rerun blindly or remove a
persistent volume.

```bash
if docker volume inspect "$SUPER7_POSTGRES_VOLUME" >/dev/null 2>&1; then
  echo 'Fresh volume name already exists; stop and inspect.' >&2
  exit 1
fi
docker volume create "$SUPER7_POSTGRES_VOLUME" >/dev/null
docker compose -f docker-compose.server.yml up -d postgres
docker compose -f docker-compose.server.yml --profile maintenance run --rm \
  db-admin python /app/scripts/server_database.py provision-fresh
```

**Existing application database:** do not run `provision-fresh`. Inventory
the database, roles, current Yoyo revision, sessions, and backup with the
DBA. The migration tool refuses an unknown/noncontiguous history, a
nonempty schema without Yoyo history, a database not owned by the migration
role, unexpected public relations or owners, unsafe role attributes or
memberships, or other active database sessions. This route assumes the DBA
has already provisioned distinct roles and transferred ownership of the
existing Super7/Yoyo relations to the migration role after review.

```bash
docker volume inspect "$SUPER7_POSTGRES_VOLUME" >/dev/null
docker compose -f docker-compose.server.yml up -d postgres
docker compose -f docker-compose.server.yml --profile maintenance run --rm \
  db-admin python /app/scripts/server_database.py inspect-version
```

For either path, stop all API/worker/dispatcher writers. For an existing
database, inspect its current applied revisions before the window. Back up
to a private location outside the checkout; the archive listing checks
readability, and a test restore on an isolated database must establish usable
recovery before setting `SUPER7_BACKUP_CONFIRMED=1`:

```bash
docker compose -f docker-compose.server.yml stop -t 1800 worker
docker compose -f docker-compose.server.yml --profile dispatch stop -t 120 dispatcher
docker compose -f docker-compose.server.yml stop -t 30 api
backup_dir=/opt/super7-backups
sudo install -d -m 0700 -o "$(id -u)" -g "$(id -g)" "$backup_dir"
umask 077
backup="$backup_dir/super7-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose -f docker-compose.server.yml exec -T postgres \
  pg_dump -U super7_admin -d super7 -Fc > "$backup"
test -s "$backup"
docker compose -f docker-compose.server.yml exec -T postgres \
  pg_restore --list < "$backup" > /dev/null
```

After the separate restore check, run the explicit migration:

```bash
docker compose -f docker-compose.server.yml --profile maintenance run --rm \
  -e SUPER7_WRITERS_STOPPED=1 -e SUPER7_BACKUP_CONFIRMED=1 \
  db-admin python /app/scripts/server_database.py migrate
docker compose -f docker-compose.server.yml --profile maintenance run --rm \
  db-admin python /app/scripts/server_database.py verify-grants
```

The target is `sprint2_slice5_video_integrity`. The tool grants API only
admission table read/insert, worker the job/attempt mutations and terminal
outbox insertion, and dispatcher outbox delivery-column updates. Each role
gets Yoyo version read access and no schema create privilege. The tool
removes `PUBLIC` and direct table/sequence grants before granting
the reviewed matrix, then checks permitted and forbidden table/column rights
using each actual login. An adopted database with unrelated consumers needs a
separate privilege review before this operation.
On failure, keep writers stopped, preserve the volume, inspect the schema
and migration log, and restore from the verified backup if required.
Application code rollback does **not** reverse a database migration.

## Controlled startup and activation

Start only API and the single worker after schema and role checks:

```bash
docker compose -f docker-compose.server.yml up -d api worker
docker compose -f docker-compose.server.yml ps
curl --fail --silent http://127.0.0.1:18080/health/ready
docker compose -f docker-compose.server.yml logs --tail=50 worker
```

From an operator workstation, use an SSH tunnel for loopback admission
validation: `ssh -L 18080:127.0.0.1:18080 administrator@108.181.226.211`.
The API readiness probe checks its database and schema, not worker capacity.
The headless worker and dispatcher have no HTTP readiness endpoint; their
`durable_worker_ready` / `durable_dispatcher_ready` events, database ownership
checks, exit status, and controlled job evidence must be inspected. Compose
therefore does not fake their readiness with a process-exists healthcheck.

Do not submit a request that can produce an Apex callback until Apex
`invalid_video` compatibility, the backend integrity policy, the deferred
generic R2 404/NotFound classification, and live R2/GPU/receiver behavior
have been reviewed. This Compose file forces
`SUPER7_DURABLE_CALLBACK_HTTPS_ONLY=1` in both API and dispatcher. The API
returns 422 for an HTTP callback URL before admission. The dispatcher rejects
an older HTTP outbox destination before any network connection and records
the existing `RETRYING` state with a 30-second due delay; it never marks that
event delivered. Other development configurations retain their prior policy.

Before dispatcher activation, identify older HTTP records without printing
their signed URLs. Run this only in the approved maintenance window:

```bash
docker compose -f docker-compose.server.yml exec -T postgres \
  psql -U super7_admin -d super7 -v ON_ERROR_STOP=1 \
  -c "SELECT o.callback_event_id, o.job_id, o.delivery_state, o.next_attempt_at
      FROM analysis_callback_outbox AS o
      JOIN analysis_jobs AS j ON j.job_id = o.job_id
      WHERE j.callback_url ~* '^http://' AND o.delivery_state <> 'DELIVERED'
      ORDER BY o.created_at;"
```

Review any returned IDs with the callback owner under an approved remediation
plan. Do not edit the immutable URL, substitute HTTPS, mark a record delivered,
or send it manually as a shortcut. Keep dispatch disabled until the HTTPS
receiver and handling of older HTTP records are agreed. When callback delivery
is explicitly authorized, activate
the dispatcher as a separate step:

```bash
docker compose -f docker-compose.server.yml --profile dispatch up -d dispatcher
docker compose -f docker-compose.server.yml logs --tail=50 dispatcher
```

No `docker compose down -v`, volume delete, trial bootstrap, root Compose
`up`, or deployment workflow dispatch belongs in this procedure. The worker
has one spawned analysis child; an ordinary graceful stop may wait for native
work. Compose allows 30 minutes before force kill, which must be compared to
measured worst-case inference and cleanup. The dispatcher gets two minutes
to settle an in-flight delivery. Failed configuration exits are limited to
three automatic restart attempts rather than hidden indefinitely.
