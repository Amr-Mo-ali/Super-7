> Status: Blocked infrastructure decision register. No resource has been provisioned or inspected.

# Sprint 2 infrastructure decisions required

The proposed architecture requires an operational boundary, but this document contains no
credentials, endpoints, hostnames, or deployment instructions. PostgreSQL and durable artifact
storage remain proposals until the owners approve and provide them.

| Decision | Recommended option | Alternatives requiring explicit selection | Consequence if unresolved | Owner | Blocks slice |
|---|---|---|---|---|---|
| Super-7 database/schema | Provide a Super-7-owned logical database or isolated schema and least-privilege role; never use Apex application tables as the queue. | Separate physical PostgreSQL service; isolated database on an approved shared PostgreSQL server. | Schema ownership, blast radius, migrations, and backup responsibility remain ambiguous. | Infrastructure | 1–11 |
| Connection and secret ownership | Infrastructure supplies credentials through the approved runtime secret mechanism; Super-7 documents only variable names and required privileges. | Separate credentials per API/worker/dispatcher role. | Secure configuration and least privilege cannot be implemented or reviewed. | Infrastructure | 1, 8, 11 |
| Migration execution | One controlled, observable release/operator step runs reviewed migrations before activating dependent code; application instances do not race migrations at ordinary startup. | A dedicated migration job under equivalent serialization and approval. | Concurrent or partial migration can make API and worker versions incompatible. | Shared | 1, 8, 9, 11 |
| Backup and restore | Define encrypted backups, retention, restore ownership, and tested recovery objectives before production cutover. | Provider-managed backups with an independently tested restore; self-managed backup under approved operations. | PostgreSQL becomes a nominal durable store without a demonstrated recovery path. | Infrastructure | 1, 8, 10, 11 |
| Availability expectations | Define the accepted behavior and operational objective for database outages; admission should fail before acceptance when durability is unavailable. | Controlled degraded read-only/status behavior if later required. | Readiness, alerting, lease behavior, and outage response cannot be finalized. | Shared | 1, 3, 8, 10, 11 |
| Connection-pool constraints | Set a total database connection budget, then allocate bounded pools across every API, worker, callback, migration, and maintenance role. | External pooler after measured need; direct small pools initially. | Horizontally started processes may exhaust PostgreSQL even at low job concurrency. | Shared | 1, 3, 5, 8, 11 |
| Shared database reachability | Every API instance and the one initial worker and callback role reach the same authoritative database through the approved network boundary. | Co-located roles using the same database; later multi-host private connectivity. | API replacement and worker/callback recovery cannot share authoritative state. | Infrastructure | 1, 3, 5, 8, 10, 11 |
| Durable artifact root | If retained diagnostics must survive process or host replacement, provide one approved durable/shared root or object store with explicit access and quota ownership. | Worker-local persistent namespace with restricted retention; no retained artifacts. | PostgreSQL metadata may outlive unreachable bytes, and cross-process cleanup cannot be correct. | Shared | 7, 8, 10, 11 |
| Behavior without durable artifact storage | Keep retention disabled and treat temporary analysis artifacts as disposable; artifact loss must not alter durable results or callbacks. | Block debug retention while allowing analysis; block the debug-enabled mode entirely. | The service may imply retained diagnostics it cannot recover or bound after replacement. | Super-7 | 7, 8, 10, 11 |

## Stop boundary

No database dependency, migration, connection setting, service definition, or deployment change is
authorized until the database/schema, secret, migration, connection-budget, and reachability rows
are approved. Artifact reconciliation must not be implemented until the artifact-root and
unavailable-storage behavior are selected. Slice 11 production activation additionally requires
all applicable infrastructure rows, migration/rollback rehearsal, and approved Slice 10 crash
evidence.
