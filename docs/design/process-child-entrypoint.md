# Process child entry point

MVP-2B2 provides unused top-level child contracts, initializer, and synchronous callable. `Settings` crosses because it is an explicit immutable dataclass with no credentials today; adding secrets requires boundary review.

The initializer constructs one lazy composition graph, safe resolver, and artifact manager. Each job validates/resolves its filename below the configured storage root, creates and cleans child-local artifacts, serializes a public analysis response, and returns only fixed sanitized failure/cancellation envelopes. It never creates callbacks or callback payloads.

The calculation temporarily calls the established `_analyze_uploaded` façade rather than copying scoring logic. MVP-2C must add the experimental one-worker ProcessPool wiring and parent result handling; production remains in-process with no runtime switch.
