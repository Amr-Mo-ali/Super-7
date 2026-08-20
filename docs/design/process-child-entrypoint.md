# Process child entry point

MVP-2B2 provides unused top-level child contracts, initializer, and synchronous callable. `Settings` crosses because it is an explicit immutable dataclass with no credentials today; adding secrets requires boundary review.

The initializer constructs one lazy composition graph, safe resolver, and artifact manager. Each job validates/resolves its filename below the configured storage root, creates and cleans child-local artifacts, serializes a public analysis response, and returns sanitized failure/cancellation envelopes. Parent validation checks the expected analysis ID and schema before converting a success to the existing response union or a failure/cancellation to typed parent outcomes. Cleanup failure cannot leak an exception: it converts a successful result to a sanitized failure and preserves an existing failure/cancellation outcome.

The local `CancellationManager` is not currently signalled across a process boundary. `ChildAnalysisCancelled` therefore represents cooperative cancellation raised internally or by tests, not active parent-to-child cancellation. MVP-2C must decide whether Future cancellation stops only waiting or whether a shared primitive is warranted. Initialization logs only the child PID, analysis version, and execution mode; model paths are not logged.

The calculation temporarily calls the established `_analyze_uploaded` façade rather than copying scoring logic. MVP-2C must add the experimental one-worker ProcessPool wiring and parent result handling; production remains in-process with no runtime switch.
