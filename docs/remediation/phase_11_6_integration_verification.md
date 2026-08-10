# Phase 11.6 - End-to-end backend integration verification

## Architecture

```text
Backend -> shared /data/videos volume -> POST /analyze -> HTTP 202
                                                |
                                                v
                                      bounded FIFO queue
                                                |
                                                v
                                    one analysis worker -> callback -> backend update
```

The backend owns video storage and persistence. Super-7 receives only the relative video filename,
resolves it below its read-only `/videos` mount when work begins, runs the existing pipeline, and
delivers the final callback. No database client or database connection is present in Super-7.

## Integration harness and deployment

`docker-compose.integration.yml` provides two services:

- `backend-mock`: a minimal callback receiver with `POST /webhook`, `GET /callbacks`, and
  `DELETE /callbacks`. It records received callbacks and simulates a backend database update keyed
  by `video_id`.
- `super-7`: the production container with `/data/videos:/videos:ro` and
  `VIDEO_STORAGE_ROOT=/videos`.

Prepare a readable host directory and models, then start the isolated harness:

```powershell
New-Item -ItemType Directory -Force /data/videos
docker compose -f docker-compose.integration.yml up --build
```

Inspect readiness with `GET http://localhost:8000/health/ready` and mock callbacks with
`GET http://localhost:8081/callbacks`. Reset mock state with `DELETE http://localhost:8081/callbacks`.

## Verification procedure

The automated Phase 11.6 tests exercise the full Super-7 HTTP request, queue, single worker,
existing pipeline boundary, callback payload, and backend database-update simulation. They verify:

1. valid request acceptance, storage resolution, `202`, worker execution, final callback, and
   readiness;
2. a file that disappears after queue admission yields one sanitized `failed` callback;
3. a bounded queue returns `503` without executing or callback-delivering the rejected job;
4. callback delivery failure leaves the analysis job `COMPLETED` and emits an explicit log;
5. accepted waiting jobs become `CANCELLED` on restart/shutdown because the queue is non-durable;
6. multiple queued requests complete and callback in FIFO order; and
7. callback-mock inspection endpoints record the received payload and simulated database update.

Run the verification and quality gates:

```powershell
uv run pytest -q tests/integration/test_phase_11_6_backend_flow.py
uv run pytest -q
uv run mypy src tests
uv run ruff check .
uv run ruff format --check .
```

## Expected logs

Successful work emits, in order, `analysis_job_queued`, `analysis_job_started`, and
`analysis_job_completed`. A pipeline error emits `analysis_job_failed` and produces a sanitized
failure callback. An exhausted callback retry policy emits `analysis_callback_failed`; that is a
delivery outcome, not an analysis failure.

## Security constraint for Docker-local callbacks

The production callback service correctly rejects loopback and private IP destinations to prevent
SSRF. Docker service names such as `http://backend-mock:8080/webhook` resolve to a private Docker
network address and are therefore intentionally rejected. Do not disable this rule for integration.

For a manual Compose delivery test, provide a security-approved, externally reachable HTTP(S)
callback ingress that resolves only to public IP addresses, then inspect forwarded deliveries at the
mock or real backend. The automated harness uses an injected in-process transport solely to simulate
the backend update while retaining the production callback validation behavior in its dedicated
tests.

## Known limitations and rollback

The queue is in-memory and non-durable: a restart cancels accepted waiting work, and a client must
resubmit it. The integration Compose file does not supply test videos or model weights; those remain
deployment-owned host assets. The mock database is intentionally in-memory.

Rollback is configuration-only: stop the integration stack with
`docker compose -f docker-compose.integration.yml down`, then deploy the previous Super-7 image and
its matching Compose configuration. No Super-7 database migration or persistent state rollback is
required.

## Next recommended phase

Design durable, idempotent job ownership and callback delivery before introducing multi-process or
multi-replica execution. That design must retain the existing single-model-execution safety boundary
until a separately validated concurrency model exists.
