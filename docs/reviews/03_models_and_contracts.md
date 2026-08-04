# Models and contracts review — Phase 3

Scope: internal dataclasses/DTOs/protocols/enums/result objects/diagnostics/event
candidates, and public Pydantic response models. No algorithm behavior was changed.

## Changes applied

- Added `tests/test_model_contracts.py`.
- The tests protect existing domain immutability, JSON serialization of coordinate tuples,
  and API quality-range validation. No production contract field or algorithm changed.

## Findings

### Critical

None found.

### High

#### Large diagnostics payloads rely on untyped dictionaries

`TechnicalEventDiagnostics` contains multiple `dict[str, float]`, `dict[str, int]`, and
`tuple[dict[str, float | int | bool | str | None], ...]` fields. `TrackingDiagnostics` also
uses `tuple[dict[str, object], ...]`; public `Diagnostics` mirrors several such fields.
These are typed only at the container level, so field names, units, optionality, and
serialization shape are not enforced.

Impact: a typo or incompatible value in diagnostic assembly is discoverable only in runtime
tests or client parsing. This is the main model-contract risk in the reviewed scope.

### Medium

#### The public response permits some contradictory states

Examples:

- `SelectedPlayer.segment_start_frame` may be populated while `segment_end_frame` is absent.
- `PassDetectionResponse` and `ShotDetectionResponse` expose raw/accepted/rejected counters
  with no model-level reconciliation invariant.
- `FeatureMetric` allows both `value` and `reason` to be `None`, leaving an unspecified
  state rather than explicitly unavailable/available.
- Event candidates expose `start_frame`, `release_frame`, and `end_frame` without Pydantic
  ordering constraints.

These models preserve current producer behavior, but external construction can create
invalid combinations.

#### Unit names are not consistently encoded in field names

Strong examples exist (`timestamp_seconds`, `*_pixels`, `*_ratio`, `processing_time_ms`),
but several fields are ambiguous: `MovementMetrics.covered_distance`, `average_speed`,
`maximum_speed`, `BallLossCandidate.maximum_separation_ratio` contextually, pass/shot
`distance`, and `release_speed`. Current implementation implies image-space pixels for many
of them, but the public response sometimes duplicates clearer `*_pixels` fields.

#### Internal status representations mix `Literal`, `str`, and booleans

Candidate model statuses use narrow `Literal` values, which is good. Other contracts use
unconstrained `str` (`PhysicalScoreResult.status`, `FeatureMetric.reason`, `ShotCandidate`
status), booleans (`accepted_by_ball_tracker`, `recovered_within_window`, transform
acceptance), and `InteractionState = Literal[...]`. This is not a direct defect, but it
makes exhaustiveness and serialization contracts uneven. No enum conversion was made because
that could alter current serialization/type acceptance without a demonstrated consumer need.

#### Technical-event protocols are too weak and duplicate intent

`TechnicalEventAnalyzerProtocol.analyze(self, *args: object, **kwargs: object)` offers no
usable contract. `ControlledMovementDetectorProtocol`, `DribbleCandidateDetectorProtocol`,
and `BallLossCandidateDetectorProtocol` are empty marker protocols and have no concrete
consumer identified in the reviewed source. This is weaker than the explicit interaction,
detector, and scorer protocols.

### Low

#### Model duplication / near-duplication

- `BoundingBox` is shared effectively across player and ball models, which is good.
- Domain candidates (`PassCandidate`, `ShotCandidate`, technical-event dataclasses) are
  mirrored by API-specific Pydantic response models. This is appropriate at the boundary,
  but mapping is manual and field drift is possible.
- `PlayerObservation`, `MovementPoint`, `BallTrackPoint`, and `BallObservation` all encode
  frame/time/location evidence with intentionally different purposes; their overlapping
  names/units need documentation rather than forced unification.

#### Mutable API models are intentional but not configured defensively

Internal computation models inspected are frozen slot dataclasses. Pydantic response models
are mutable by default, and use mutable lists/dictionaries. That matches ordinary FastAPI
response assembly, but they should not be treated as shared/cacheable objects.

#### Response schemas do not expose raw internal dataclasses directly

Routes map dataclasses with `asdict()` into Pydantic response models, so FastAPI itself is not
coupled into domain models. `dict[str, object]` diagnostics are the exception: internal
dictionary shape is exposed without a distinct boundary DTO.

#### Result versioning is uneven

Interaction, technical score, physical score, segment ball, pass, shot, and camera-motion
outputs carry versions. Core domain objects such as `MovementResult`, `TrackingRun`, and
`TechnicalEventAnalysisResult` do not themselves carry contract versions; versions are
assembled later in route diagnostics/response fields.

## Protocol and dependency observations

- `PlayerDetectorProtocol`, `BallDetector`, `AutomaticPlayerTracker`,
  `BallInteractionAnalyzerProtocol`, and `PhysicalActivityScorerProtocol` are small,
  explicit dependency contracts.
- No abstract base class duplication was found; protocols are used rather than ABCs.
- `TechnicalEventAnalyzerProtocol` should not be relied on for type safety until it has a
  concrete method signature and a real injected consumer. This review does not add an
  abstraction merely for consistency.

## Serialization observations

- Pydantic serializes `tuple[float, float]` coordinate values as JSON arrays; this is now
  covered by a contract test.
- Bounds are consistently applied to many confidence/quality response fields but not to
  every ratio, count, duration, or event-frame relationship.
- `asdict()` converts frozen domain dataclasses to mutable nested dictionaries/lists before
  Pydantic validation. This is normal boundary mapping, but makes the untyped diagnostic
  dictionaries particularly significant.

## Exact modified files

- `tests/test_model_contracts.py` — three additive contract/serialization tests.
- `docs/reviews/03_models_and_contracts.md` — this review report.

## Remaining risks

1. Untyped diagnostic maps can drift silently while public response fields remain valid.
2. Event/frame/counter cross-field invariants rely on producers, not schemas.
3. Ambiguous unit naming complicates clients that compare raw and compensated metrics.
4. Empty/variadic technical-event protocols provide little compile-time protection.
5. API/domain mirror models require manual mapping maintenance.
