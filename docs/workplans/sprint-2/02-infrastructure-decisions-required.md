> Status: Local PostgreSQL foundation authorized; staging and production infrastructure decisions
> pending. No production resource has been provisioned or inspected.

# Sprint 2 infrastructure decisions required

The accepted architecture requires an operational boundary, but this document contains no
credentials, endpoints, hostnames, or deployment instructions.

For local Slice 1, Super-7 may use disposable PostgreSQL, task-scoped local credentials, disposable
runtime and migration roles, bounded local pool defaults, and local apply/rollback/reapply
verification. TLS may be disabled only for an explicitly disposable local database. No credential
value may be committed. Slice 1 creates no domain tables.

Staging and production still require an approved database/schema, real least-privilege runtime and
migration roles, approved secret delivery, TLS verification, shared reachability, an approved
connection budget, backup/restore ownership and evidence, migration/rollback rehearsal, and
operational monitoring and outage ownership. No such resource or evidence is claimed here.
There is also no evidence of a restore test or an implemented RPO/PITR/WAL policy. Daily snapshots
alone must not be described as satisfying a one-hour RPO.

| Decision | Recommended option | Alternatives requiring explicit selection | Consequence if unresolved | Owner | Blocks local implementation? | Blocks staging integration? | Blocks production cutover? |
|---|---|---|---|---|---|---|---|
| Disposable PostgreSQL and migration tooling | Use a task-scoped disposable database, credentials, and roles for local tooling and apply/rollback/reapply verification. | An approved equivalent disposable harness. | Slice 1 behavior cannot be verified without touching an external environment. | Super-7 | Required for Slice 1 verification. | Required equivalent. | Required equivalent plus target-environment rehearsal. |
| Super-7 database/schema | Provide a Super-7-owned logical database or isolated schema and least-privilege role; never use Apex application tables as the queue. | Separate physical PostgreSQL service; isolated database on an approved shared PostgreSQL server. | Schema ownership, blast radius, migrations, and backup responsibility remain ambiguous. | Infrastructure | No; use disposable PostgreSQL. | Yes. | Yes. |
| Connection and secret ownership | Infrastructure supplies real runtime and migration credentials through the approved runtime secret mechanism; Super-7 documents only variable names and required privileges. | Separate credentials per API/worker/dispatcher role. | Secure target configuration and least privilege cannot be implemented or reviewed. | Infrastructure | No; use task-scoped disposable roles and credentials. | Yes. | Yes. |
| TLS verification | Verify the target certificate and connection policy through the approved trust boundary. | An explicitly approved equivalent private-network policy. | Target database transport security remains unproven. | Infrastructure | No for an explicitly disposable local database. | Yes. | Yes. |
| Migration execution | One controlled, observable release/operator step runs reviewed migrations before activating dependent code; application instances do not race migrations at ordinary startup. | A dedicated migration job under equivalent serialization and approval. | Concurrent or partial target migration can make API and worker versions incompatible. | Shared | No; local tooling must still prove apply/rollback/reapply. | Yes. | Yes. |
| Backup and restore | Define encrypted backups, retention, restore ownership, and tested recovery objectives before production cutover. | Provider-managed backups with an independently tested restore; self-managed backup under approved operations. | PostgreSQL becomes a nominal durable store without a demonstrated recovery path. | Infrastructure | No. | May remain pending for isolated staging if explicitly accepted. | Yes. |
| Availability expectations | Define the accepted behavior and operational objective for database outages; admission should fail before acceptance when durability is unavailable. | Controlled degraded read-only/status behavior if later required. | Readiness, alerting, lease behavior, and outage response cannot be finalized. | Shared | No; deterministic local outage tests may proceed. | Needed for realistic staging. | Yes. |
| Connection-pool constraints | Set a total target connection budget, then allocate bounded pools across every API, worker, callback, migration, and maintenance role. | External pooler after measured need; direct small pools initially. | Horizontally started processes may exhaust PostgreSQL even at low job concurrency. | Shared | No; use validated bounded local defaults. | Yes. | Yes. |
| Shared database reachability | Every API instance and the one initial worker and callback role reach the same authoritative database through the approved network boundary. | Co-located roles using the same database; later multi-host private connectivity. | API replacement and worker/callback recovery cannot share authoritative state. | Infrastructure | No. | Yes. | Yes. |
| Durable artifact root | If retained diagnostics must survive process or host replacement, provide one approved durable/shared root or object store with explicit access and quota ownership. | Worker-local persistent namespace with restricted retention; no retained artifacts. | PostgreSQL metadata may outlive unreachable bytes, and cross-process cleanup cannot be correct. | Shared | No for Slice 1. | Required only for retained-debug integration. | Required if retention is enabled. |
| Behavior without durable artifact storage | Keep retention disabled and treat temporary analysis artifacts as disposable; artifact loss must not alter durable results or callbacks. | Block debug retention while allowing analysis; block the debug-enabled mode entirely. | The service may imply retained diagnostics it cannot recover or bound after replacement. | Super-7 | May be implemented locally in its own slice. | Must be explicit. | Must be explicit. |

## Stop boundary

Local Slice 1 dependency, typed configuration, connectivity, bounded-pool, schema-version, and
migration-tooling work is authorized against disposable PostgreSQL. Domain migrations remain Slice
2. Staging connections and real-role activation wait for target infrastructure evidence.
Production database creation and migration execution remain separately authorized operational
actions. Artifact reconciliation remains gated by its own slice and storage decision. Slice 11 is
the only production activation boundary and additionally requires all applicable infrastructure
rows, migration/rollback rehearsal, and approved Slice 10 crash evidence.
