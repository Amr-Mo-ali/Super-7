# ADR-012 — Explicit runtime selection

Status: implemented for runtime selection; production role deployment remains unapproved.

The legacy in-memory `main:app` and `main.create_app()` composition require
`SUPER7_LEGACY_IN_MEMORY_ENABLED=1`. Direct `create_router()` composition without
durable admission has the same gate. The flag is intended for development and
offline tests, not ordinary deployment. Without it, `main:app` has no
`/analyze` route and its lifespan fails before accepting traffic. Importing
`main` remains safe so the combined durable factory can reuse its composition
without starting legacy actors; supplying `durable_runtime` bypasses only the
legacy gate and retains the existing durable admission route.

Both images exit with a clear error when launched without an explicit role
command. The separated local trial continues to override its image command for
the API, worker, and dispatcher. Integration Compose explicitly opts into the
legacy runtime for its synthetic test flow. Root Compose has no selected
production role, and the manual deploy workflow stops before SSH. The workflow
is triggered only by `workflow_dispatch`; this decision does not activate an
automatic deployment.

Production activation requires a separately reviewed API, worker, and
dispatcher topology, caller/rollback compatibility, role credentials, and
exclusive actor operation. The local trial Compose file is not that topology.
No request or callback schema, score, persistence transaction, worker fence,
or process count changes in this decision.
