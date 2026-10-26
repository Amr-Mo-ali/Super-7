# Production environment: local configuration proposal

This file describes a future, separately approved deployment. Nothing in this
change starts a service, creates a database, or changes the Development stack.
The source is the existing root `Dockerfile` and the existing durable API,
worker, and dispatcher entry points. Use `docker-compose.production.yml` alone,
with project name `super7-production`; never combine it with the Development or
local-trial Compose files.

## Boundaries and decisions

- Production uses `super7_production`, with new `super7_production_migration`
  and `super7_production_runtime` roles. Its API binds only to
  `127.0.0.1:18081`. The external `super7-net` network reaches the existing
  `super7-postgres` service; the manifest creates no PostgreSQL service or
  published database port. PostgreSQL, its `super7-pgdata` volume, network,
  host, A4000, and R2 bucket remain shared. A separate database name does not
  grant access isolation.
- API, worker, and dispatcher have distinct private environment files outside
  the checkout and Docker build context. All three need the Production runtime
  database password. Only `api.env` contains `SUPER7_APEX_BEARER_SECRET` and
  `SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX`; only `worker.env` contains the three
  `SUPER7_R2_*` values. The dispatcher receives neither set. Use new secrets,
  never Development values. Set restrictive file ownership and permissions.
- Both environments use the approved `4goats-media-dev` R2 bucket, which the
  worker code selects directly. Production needs its own read-only token scoped
  to that bucket; never reuse the Development or backend write token. Confirm
  that the Production backend uploads to `videos/` in this bucket before worker
  activation. R2 video objects are shared, not environment-isolated.
- The worker and dispatcher have opt-in Compose profiles. Do not activate them
  until the storage, callback, and GPU checks are complete. Drain Development
  work and decide GPU scheduling before starting a Production worker. Concurrent
  Development and Production workers share the A4000 and require resource and
  latency measurement before approval. Keep one Production worker; this file
  makes no analysis-concurrency increase.
- The worker alone has GPU access and the read-only `/models` mount. It has no
  local video mount or fallback. The API and dispatcher have neither. The
  chosen Production model ID must match the reviewed mounted weights. Verify
  CUDA availability and model loading in the selected clean image with dummy
  input before activation; a GPU device request does not prove CUDA support.
  The worker uses `/tmp` for its working directory and home, with Ultralytics
  settings under `/tmp/ultralytics`, and disables debug artifacts. The API
  retains its readiness healthcheck; worker and dispatcher disable the image's
  inherited HTTP healthcheck. Inspect their process state and role-ready logs
  separately from API readiness.

## Private configuration to prepare later

Copy `production.env.example` outside the checkout for Compose interpolation.
Use an inspected image digest or unique source-revision tag in
`SUPER7_PROD_IMAGE`; never rely on `football-analysis:local` moving over time.
Before activation, confirm the selected image ID and source revision and check
that `httpx` imports in the clean image. The image is built from the existing
`Dockerfile`; no role builds an image at startup.

Create three separate private files at the absolute paths in the Compose
interpolation file:

| File | Required entries read by the application |
| --- | --- |
| `api.env` | `POSTGRES_PASSWORD`, `SUPER7_APEX_BEARER_SECRET`, `SUPER7_IDEMPOTENCY_HMAC_SECRET_HEX` (hex encoded secret) |
| `worker.env` | `POSTGRES_PASSWORD`, `SUPER7_R2_ENDPOINT`, `SUPER7_R2_ACCESS_KEY_ID`, `SUPER7_R2_SECRET_ACCESS_KEY` |
| `dispatcher.env` | `POSTGRES_PASSWORD` |

Each database password is the same new Production runtime-role password.
The manifest supplies the database name, role name, pool settings, current
schema revision, and role opt-ins. `SUPER7_VIDEO_SOURCE=r2` is worker-only.
The Production R2 credential must be read-only and scoped to the shared
`4goats-media-dev` bucket. The API bearer and idempotency secrets must be
independent of Development. Missing role files fail Compose loading; missing
required application values fail at role construction/startup. Protect the private
files and any rendered `docker compose config` output as secrets.
The API attempt limit is `3`, preserving the owner-supplied deployed policy;
this configuration does not change retry, lease recovery, or failure behavior.

## Database gate before any Production admission

At a separately approved maintenance point, inspect live PostgreSQL roles,
memberships, database ACLs, and active Development access. Create the two new
Production roles and `super7_production` owned by the migration role. Revoke
`PUBLIC` database `CONNECT`/`TEMP` where needed and grant only intended roles.
Check Development grants before any change to its ACL: the ongoing concurrency
experiment must not lose access. In the Production database, revoke public
schema creation, grant runtime schema usage, apply the packaged Slice 1–5
Yoyo migrations with the Production migration owner, then grant the runtime
role the table/column privileges used by the existing
`scripts/durable_compose_setup.py` bootstrap. Do not invoke that trial
bootstrap against the shared instance and do not run migrations at app startup.

Verify both directions with `has_database_privilege(role, database, 'CONNECT')`:
Production migration/runtime roles may connect only to Production, and
Development migration/runtime roles only to Development. Inspect inherited
memberships and superuser attributes; database `CONNECT` alone is not the
entire isolation boundary. Check that the Production runtime role has schema
usage and exactly the required table/column grants, no schema creation or
table deletion; perform a read/write smoke check with disposable Production
rows in an approved maintenance session. Also verify backup, logs, and admin
access policies for the shared cluster. None of these live checks is implied
by the Compose file.

## Reviewable activation, upgrade, and rollback

1. Confirm the Development topology and active experiment, image identity,
   external network attachment, database permission gate, Production model
   weights, the backend upload destination and Production read-only R2 access,
   caller routing and callback contract, plus GPU capacity and Development
   work drainage. Keep Domain/Nginx/TLS changes for the final deployment
   step. Use dummy values first to render the effective manifest with
   `docker compose --env-file /private/compose.env -f docker-compose.production.yml --profile processing --profile delivery config --quiet`.
   Review the rendered role environments securely; never paste their values
   into logs or review artifacts.
2. After separate deployment approval, start `api` explicitly with the same
   Compose arguments and `up -d --no-deps api`. Confirm loopback readiness on
   port 18081 and database identity before routing Production callers. Start
   `worker` and `dispatcher` by explicit service name only after their gates
   pass. Confirm exactly one of each, inspect exits and role-ready events,
   and verify no Development port or container is replaced.
3. For an upgrade, pause Production admission/callers, inspect queued and
   running jobs plus pending callbacks using an approved read-only database
   procedure, and drain before replacing roles. Record the old image ID and
   select and verify a new immutable image. Apply packaged migrations under
   the migration role only when required, with a reviewed compatibility and
   rollback plan. Restart one role at a time and check readiness, outbox,
   image identity, and resource use before resuming callers.
4. For rollback, pause Production callers, drain or explicitly account for
   jobs and callbacks, stop Production API, dispatcher, and worker, and
   restore the recorded image/configuration only if it is schema-compatible.
   Verify all role exits before starting a replacement; never overlap two
   dispatchers for this database. Preserve both databases and the shared
   volume. If a migration is not backward-compatible, use its reviewed data
   recovery plan rather than automatically reversing it.

The worker's 180-second Compose stop grace is a supervisor allowance, not a
proof that native inference terminates within it. Child shutdown may wait
longer; forceful termination can leave a lease to recover. The dispatcher
needs time to finish in-flight delivery. Observe pending work before stopping
either role and check recovery and callback state after restart.
