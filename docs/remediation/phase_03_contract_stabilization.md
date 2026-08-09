# Phase 3: Contract Stabilization & Public Interface Hardening

## Objective

Stabilize the existing V1 analysis and V2 public-rating boundaries without changing analysis algorithms, score formulas, arbitration decisions, field names, or response semantics.

## Previous and new state

V1 remains owned by `schemas.analysis`; V2 remains owned by `schemas.public_rating_v2`; arbitration owns immutable domain event contracts; `api.public_rating_mapper` is the one-way presentation adapter. Previously the mapper contained `object` helpers and duplicate legacy raw-event serializers after V2 adopted the arbitrated timeline. The mapper now consumes concrete V1 schema and arbitration types, and obsolete raw-event helpers are removed.

## Ownership rules and compatibility guarantees

Domain `ArbitratedEvent`/`ArbitrationResult` models remain framework-neutral. Presentation `PublicEvent`, `PublicRatingValue`, and `PublicGameIntelligence` own JSON validation. V2 rating status and event type/status values are explicit literals. `0` remains an available numeric value; `None` with `insufficient_evidence`/`unsupported` remains unavailable; ambiguous and unresolved are event states. No diagnostics, trajectories, or filesystem paths were added to V2.

V1 field structure remains unchanged. V2 retains its object shape, timeline field, event IDs, status values, and versions. Deterministic event ordering remains owned by `EventArbitrator`; Pydantic serialization is tested repeatedly for stable output.

## Migration notes, tests, risks, lessons

There is no client migration. Invalid unknown V2 rating/event state values now fail validation rather than serializing arbitrary strings; mapper-generated existing states are preserved. Tests cover V1 supported/unsupported serialization, V2 zero versus insufficient evidence, ambiguous timeline serialization, unavailable game-intelligence components, deterministic JSON, and existing V1/V2/arbitration suites.

Risk remains in the intentionally large V1 diagnostics model and dictionary-shaped V1 diagnostics/quality fields; changing those would be a public-contract redesign and is deferred. The lesson is to use immutable domain contracts internally and explicit presentation literals at the final serialization boundary.
