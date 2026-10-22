# ADR-015 — Prepared separated R2 server topology

Status: prepared configuration; production activation is not approved.

The server candidate uses one durable admission API, one worker with the
existing single spawned analysis child, one callback dispatcher, and
PostgreSQL 16 with persistent storage. `docker-compose.server.yml` selects
R2 explicitly for the worker, mounts only models read-only, keeps the API on
host loopback, and makes dispatcher activation a separate profile. The worker
alone receives the R2 read credential and GPU device. The runtime roles have
separate database logins; runtime services never use the migration login. Runtime
startup never applies migrations.

The root and local-trial Compose files retain their prior fail-closed and
test behavior. This preparation does not enable the GitHub deployment
workflow, public admission, or callback delivery. The GPU worker image uses
the official CUDA 12.6 PyTorch pair but still requires wheel-hash review,
target-device execution, and model/inference validation. API readiness covers
its own database/schema; the headless worker and dispatcher expose no
independent health endpoint, so their ready events, database ownership, exit
status, and controlled work must be inspected rather than using a process
existence probe.

The existing database requires a verified backup, stopped writers, known
contiguous Yoyo history, and ownership checks before the explicit Slice 5
migration. Application rollback does not reverse schema changes. The deferred
generic R2 404/NotFound classification, backend integrity policy, Apex
`invalid_video` handling, and live R2/GPU/callback checks remain rollout
gates. This decision does not change analysis, callback, or migration history.
