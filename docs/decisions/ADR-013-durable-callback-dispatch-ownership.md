# ADR-013 — Durable callback dispatcher ownership

Status: accepted for the opt-in durable runtimes.

The durable callback outbox has authoritative conditional delivery updates, but
two dispatchers can read the same pending event and both send HTTP before either
update. A conditional write after HTTP cannot prevent that duplicate send.

Use a PostgreSQL **session advisory lock** with the stable two-integer identity
`(1396113410, 2)` for callback dispatch. The first integer is Super-7's existing
advisory namespace; resource `1` is used by admission transaction locking, so
resource `2` is reserved for dispatch. Every dispatcher pointed at the same
Super-7 database uses this key. The owning connection is dedicated, autocommit,
and never borrowed from or returned to the general pool. Acquisition precedes
the actor's first due-event read. A contender fails startup and closes its
session; it does not deliver. Graceful shutdown drains the active delivery,
closes the ownership session, then closes the pool.

The dedicated connection uses `POSTGRES_CONNECT_TIMEOUT_SECONDS` for the
driver connection attempt. A client-side deadline from
`POSTGRES_POOL_OPEN_TIMEOUT_SECONDS` covers connection and lock acquisition;
`POSTGRES_POOL_ACQUIRE_TIMEOUT_SECONDS` bounds each ownership check; and
`POSTGRES_POOL_CLOSE_TIMEOUT_SECONDS` bounds the attempted close. These reuse
existing validated settings without changing pool size or retry scheduling.
On timeout, cancellation, or uncertain verification, the object rejects later
checks and does not reconnect or reacquire. Startup and shutdown attempt to
close their independently owned resources even after an earlier failure. A
failed or timed-out close leaves release unconfirmed; the role must exit rather
than claiming the advisory lock has been released. Client deadlines rely on
the Psycopg operation settling cancellation, so a broken connection that does
not settle can still extend shutdown beyond the configured deadline. A
server-side statement timeout alone would not bound such a network failure.

The actor checks that its original session still holds the lock before each
discovery, immediately before HTTP, and after HTTP returns. A missing lock,
closed connection, or failed check stops the actor and fails the role. The
connection is never silently replaced. The combined local runtime uses the
same ownership rule and acquires before starting its executor or worker. A
lost session can be detected only when a check runs:
an HTTP request already in flight may reach the receiver even after PostgreSQL
has released the lock and another dispatcher has started. The post-response
check prevents this actor from recording delivery after *detected* ownership
loss, but cannot retract a request accepted by the receiver.

This preserves the outbox payload, callback event identity, destination
validation, retry schedule, and authoritative conditional delivery updates.
It does **not** promise exactly-once delivery. A crash after receiver acceptance
but before recording `DELIVERED` can cause a retry. Apex must deduplicate by
`callbackEventId`.
