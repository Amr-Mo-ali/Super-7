# Phase 4: Feature Expansion & Tactical Intelligence Foundation

## Objective and architecture

This phase adds only framework-independent contracts for future football-intelligence work. `domain.timeline` owns ordered event records and compact evidence IDs; `domain.possession` owns explicit state vocabulary; `domain.transitions` owns link decisions; `domain.sequences` owns stable sequence IDs and sequence construction. No route, detector, scorer, arbitrator, or V1/V2 schema consumes these contracts yet.

## Ownership and transition rules

Arbitration remains the authoritative owner of public candidate resolution. A future adapter may create `TimelineEvent` records from arbitrated events, but Phase 4 does not do so. A timeline canonicalizes event ordering by inclusive temporal window and ID. Events own their source/evidence IDs, never raw trajectories.

The minimal linker supports only `pass -> reception` and `reception -> shot`. `pass -> shot` is explicitly ambiguous because reception evidence is absent; all other pairs are unsupported. This is infrastructure, not possession, reception, or tactical inference.

## Sequence lifecycle and confidence

`EventSequenceBuilder` creates positive deterministic IDs from canonical timeline order. It propagates confidence using the weakest event/transition confidence and deduplicates evidence IDs in first-seen order. Sequence records are immutable and do not mutate source events.

## Compatibility, tests, risks, lessons

V1, V2, scoring, arbitration, and API responses are unchanged. Tests cover linking, sequence creation, transition states, confidence propagation, ordering, and invariants. Risks: no current producer builds receptions, timing constraints are deliberately absent, and no tactical claim is valid yet. The lesson is to introduce stable temporal/evidence ownership before adding analytics, not to infer capabilities from incomplete video context.
