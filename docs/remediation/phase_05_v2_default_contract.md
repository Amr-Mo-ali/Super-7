# Phase 05: V2 Default Contract

## Objective

Make Public Rating JSON V2 the only response contract exposed by `POST /analyze`, while keeping
the established V1 models as internal pipeline interchange models.

## Architecture

`upload -> internal V1 analysis result -> event arbitration -> public_rating_v2 mapper -> V2 HTTP response`

The route owns the public boundary. `schemas.analysis` remains internal to the analysis pipeline;
`schemas.public_rating_v2` remains the sole public response schema. The mapper is the only adapter
between them.

## Changes

- Removed the public `response_version` selector from the route signature and OpenAPI contract.
- Declared the route response as the existing V2 success or V2 failure model.
- Mapped completed, ambiguous, and non-completed internal V1 results to V2 at the route boundary.
- Kept V1 models and mapper inputs unchanged for internal compatibility.

## Compatibility and migration

V2 fields, event serialization, timeline ordering, arbitration, scoring, tracking, and algorithms
are unchanged. Internal callers may still produce and consume V1 models. External clients that
previously requested or parsed V1 must migrate to V2; the former `response_version` query no
longer selects a response shape and V2 is returned.

## Tests

Focused coverage verifies V2 route generation, removed version selection/OpenAPI exposure,
internal V1-to-V2 mapper compatibility, V2 event serialization, deterministic timeline
serialization, and arbitration behavior. The full suite remains the final compatibility check.

## Risks and lessons learned

The intentional migration risk is limited to clients depending on the formerly public V1 payload.
Keeping the conversion at one route boundary prevents accidental V1 leakage without duplicating
analysis or altering domain behavior. A typed response model makes future route changes visible in
OpenAPI and tests.
