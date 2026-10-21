# ADR-011: Own the child analysis pipeline outside HTTP

## Status

Accepted for analysis pipeline Slice 1 at source commit `529a92c373f6f3711d91dbb0b87e41a4879e7cf9`.
This is an ownership change only. It does not approve scoring, schema, callback,
durability, process-count, or production changes.

## Context

The active spawned child in `services.process_entrypoint` imported
`api.routes._analyze_uploaded`. That function called `api.routes._completed`
and related child-stage, diagnostic, and response-assembly helpers. Completed
response assembly also imported game-intelligence calculation from
`api.public_rating_mapper`. The child therefore depended on HTTP-owned
modules directly and transitively.

## Decision

`services.analysis_pipeline` owns local-file analysis orchestration,
target resolution, scoring, child-stage timing, diagnostics, artifact
publication, and assembly of the existing internal analysis responses.
`services.process_entrypoint` calls it directly. The existing
`services.analysis_composition` continues to construct concrete detector,
tracker, and scorer dependencies; the child entry point continues to own
path resolution, result serialization, and artifact cleanup.

`services.analysis_rating_projection` owns the shared event arbitration
input and game-intelligence evidence calculation needed by both completed
response logging and the public mapper. `api.public_rating_mapper` retains
Public Rating V2 presentation and explicitly re-exports `event_arbitration`
for its existing callback-presentation caller.

`api.routes` retains HTTP admission, the legacy parent-side processor,
callback delivery and error mapping. Its legacy in-process processor calls
the same service-owned pipeline. Private pipeline test imports and
monkeypatches now point to the service owner. No API compatibility wrapper
is retained for the moved private helpers.

## Consequences

The active child import graph must not reach `api` or FastAPI, including
through composition dependencies. A focused source-import guard enforces
this while traversing the legitimate YOLO adapters in
`services.analysis_composition`.

The pipeline module still assembles large internal response and diagnostic
models alongside scoring orchestration. This connected code moved together
to remove the HTTP dependency without changing its numerical, null,
target-availability, timing, exception, artifact, or serialization behavior.
Further separation requires its own behavior-preserving slice and evidence.
