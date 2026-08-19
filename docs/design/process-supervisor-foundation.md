# MVP-2A process-supervisor foundation

MVP-2A adds unused, internal spawn-safe IPC contracts and a supervisor for exactly one child. Production CPU-heavy analysis is **not** process-isolated yet.

The future parent owns admission, queue state, callbacks, and supervision. A future child will own model construction and CPU-heavy analysis. Messages contain only immutable request identifiers, a normalized relative video reference, an explicit minimal runtime configuration, and versioned result envelopes. No paths to model binaries, callback data, credentials, sessions, exceptions, or traceback data cross the boundary.

The supervisor uses `multiprocessing.get_context("spawn")`, two bounded channels, one in-flight request, readiness handshake, bounded polling, and finite startup/response/shutdown timeouts. A malformed, stale, mismatched, timed-out, or missing response, and child death, leave it `FAILED`; only a validated matching response returns it to `READY`.

Shutdown sends a bounded sentinel attempt, then terminates and, if necessary, kills the child with bounded joins. Queue close/join operations run through a short thread boundary so they do not block the event loop. Cancelling an await cannot cancel an existing helper thread, so every queue operation has a finite timeout. Shutdown exposes whether the child remained alive, its exit code, and whether forced termination was used.

Logs contain only IDs, PID, state, sanitized outcome, exit code, and forced-termination state. This foundation does not construct models, deserialize real analysis results, run callbacks, or wire FastAPI. MVP-2B must implement child composition and parent result conversion; MVP-2C must add the experimental runtime switch and integration coverage. Concurrency remains one.
