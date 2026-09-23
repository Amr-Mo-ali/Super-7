# APX-15 result-mapping preparation

Status: Accepted canonical direction — implementation, scientific validation, and integration cutover pending

## Purpose and boundary

This document prepares a source-backed mapping discussion. It does not approve an Apex contract, change a formula, or promote any provisional signal into a validated football-ability rating. Runtime code and tests are the authority; the handoff remains the governing context, especially the evidence guardrails in [scoring and product semantics](../../handoff/scoring-and-product-semantics.md).

Only the sanitized Apex field names supplied for APX-15 were evaluated. No Apex source, credentials, infrastructure, or live inference was accessed.

The core distinction for review is:

1. An **observation** is a directly measured video quantity, such as visible frames or image-space displacement.
2. **Evidence** is a detector/tracker/event confidence or an evidence-quality gate supporting a downstream calculation.
3. A **provisional metric** is a deterministic transform of observations and evidence that has not been validated as player ability.
4. A **validated rating** requires labelled outcome data, a frozen definition, measured calibration/generalization, and an approved product claim. No current public score meets that final standard.

## Executive findings

The requested propositions are established by the current implementation:

| Proposition | Finding | Source-backed proof |
|---|---|---|
| Event confidence is exposed as skill | **Yes.** `passing_and_playmaking` and `shooting_and_finishing` are `100 × mean(candidate confidence)` after arbitration and target attribution. This is event evidence relabelled as an ability-like detailed rating. | [`DetailedRatingEngine._event_score`](../../../src/services/detailed_rating/engine.py#L70) |
| Pixel motion is exposed as fitness | **Yes.** `speed_and_fitness` is `movement_intensity × 100`; movement intensity is derived from image-space positions and a configured frame diagonal. It is neither metric speed nor physiological fitness. | [`_visible_movement_activity`](../../../src/services/detailed_rating/engine.py#L130), [`BottomCenterMovementAnalyzer`](../../../src/services/movement/analyzer.py#L18) |
| Game Intelligence makes tactical claims without GSR | **Yes.** The public projection calculates Game Intelligence from proximity, candidate events, and image-space movement. There is no GSR input. Although explanations contain caveats, the public category and component labels still imply intelligence, decisions, and spatial reasoning. | [`_game_evidence`](../../../src/api/public_rating_mapper.py#L208), [`GameIntelligenceEngine`](../../../src/services/player_rating/game_intelligence.py#L104) |
| Other-player events affect the target | **Yes, in public Game Intelligence, event summary, and timeline.** Passes and shots are collected from every arbitrated interval event without filtering `possessor_track_id` to the selected target. The separate detailed pass/shot projection does filter to the selected target. | [`_game_evidence`](../../../src/api/public_rating_mapper.py#L208), [`_event_candidates`](../../../src/api/public_rating_mapper.py#L267), [`DetailedRatingEngine._event_score`](../../../src/services/detailed_rating/engine.py#L70) |
| Rejected evidence can be relabelled `AVAILABLE` | **Yes.** Player-rating adapters accept any non-null value/confidence (and physical evidence) without checking the source result status. A rejected or unavailable source object carrying numeric data becomes category status `available`. | [`PlayerRatingEngine._technical`](../../../src/services/player_rating/engine.py#L75), [`PlayerRatingEngine._physical`](../../../src/services/player_rating/engine.py#L87) |
| Physical + Ball can produce Overall without Technical | **Yes.** Overall requires any two available categories, then renormalizes the configured weights. Physical + Ball therefore produces Overall with effective weights `0.30/0.55` and `0.25/0.55`. | [`PlayerRatingEngine._overall`](../../../src/services/player_rating/engine.py#L148), [`PlayerRatingConfig`](../../../src/services/player_rating/config.py#L24) |
| Optional Game Intelligence can cause an Overall `KeyError` | **Yes, latent.** `summarize()` adds an available Game Intelligence category, but `_overall()` has weights only for Technical, Physical, and Ball and indexes the weight map by every available category. Production currently avoids the path by not passing Game Intelligence into `summarize()`. | [`summarize`](../../../src/services/player_rating/engine.py#L35), [`_overall`](../../../src/services/player_rating/engine.py#L148), [production call](../../../src/api/routes.py#L1773) |

## End-to-end score lineage

The active success path is:

`video observations → selected dominant visual track/segment → image-space movement, proximity, and candidate events → provisional category calculations → public V2 projection → callback projection → JSON with callback aliases`

The target gate establishes only a dominant visual track. Its own source says this is “never identity verification.” Qualification uses structural validity, minimum visibility, continuous track length, and average detection confidence; ambiguity is decided by a visibility margin. See [`evaluate_dominant_target`](../../../src/services/dominant_target_selection.py#L60) and [`_qualified`](../../../src/services/dominant_target_selection.py#L169).

**P0 product-attribution limitation:** Current dominant-target selection can establish a visual analysis target but cannot prove that the selected track is the person represented by `playerId`. This remains a product-attribution limitation, not merely a naming limitation. Super-7 does not currently perform biometric identity, jersey identity, Re-ID, or requested-player verification.

### Current public category and Overall scores

| Public field | Current public claim | Actual evidence source | Transformation/formula | Unit and scale | Eligibility | Confidence meaning | Null reasons | Evidence class |
|---|---|---|---|---|---|---|---|---|
| `ratings.technical_skill` | Provisional technical-skill estimate | Selected-target controlled-movement, dribble, and ball-loss candidates; event-analysis quality | Controlled event: `0.40 confidence + 0.25 displacement + 0.20 direction similarity + 0.15 duration/2`; dribble: `0.30 confidence + 0.25 movement + 0.20 proximity persistence + 0.15 path straightness + 0.10 direction changes/3`. Mean each present event type, then equal-mean the present types; subtract loss penalty capped at `0.25`; multiply by 100. | Unitless `0..100` | At least one controlled-movement or dribble candidate. The public adapter additionally requires non-null value and confidence but does not preserve source status. | Event-analysis quality multiplied by mean positive-candidate confidence; not ability probability. | `insufficient_event_evidence`, or upstream event analysis unavailable/no positive candidate. | Provisional metric; not validated rating |
| `ratings.physical_activity` | Visible physical activity, explicitly not fitness | Selected-track bottom-centre image-space trajectory, movement quality, detector confidence, visibility, interval acceptance | `100 × (0.35 movement intensity + 0.25 active-time ratio + 0.15 visibility + 0.15 continuity + 0.10 direction component)`. | Unitless `0..100`; inputs include pixels and pixels/second normalized to frame geometry | Evidence gate requires configured movement quality, visibility, duration, observation count, and accepted-interval ratio. | Mean of movement quality, tracking detection confidence, visibility, accepted-interval ratio, and duration factor; capped at `0.75` for raw image space. It is evidence quality, not fitness certainty. | `insufficient_movement_evidence` plus gate-specific failures such as inadequate duration, observations, quality, visibility, or interval acceptance. | Provisional metric; narrow observational interpretation only |
| `ratings.ball_involvement` | Observed proximity and possible interaction involvement; not possession | Selected-target possible-ball-interaction duration/count/coverage plus controlled-movement candidate duration | `100 × clamp((possible interaction seconds + sum controlled-movement seconds) / 5)`. Durations are added and can overlap. | Unitless `0..100`; time inputs in seconds | Interaction coverage at least the configured threshold and at least one possible interaction | `clamp(interaction coverage × interaction-analysis quality)`; evidence quality only | `insufficient_interaction_evidence` when interaction result is absent, coverage is low, or count is zero | Provisional metric; not validated rating |
| `ratings.game_intelligence` | “Provisional video-based heuristic indicator,” publicly nested under Game Intelligence | Proximity/interactions, candidate-event counts/confidences, image-space motion, Technical value, and the complete arbitrated pass/shot set for the interval | Requires at least three of five components and at least 4 visible seconds; renormalizes component weights `0.30/0.20/0.20/0.15/0.15`; weighted mean on `0..100`. | Unitless `0..100` | At least 4 seconds and at least 3 available components | Weighted component evidence confidence × duration factor to 20 s × component coverage × `0.75`, capped at `0.65`; not calibrated tactical correctness | `insufficient_game_intelligence_evidence`; component-specific insufficient ball, movement, technical-event, or technical evidence | Unvalidated heuristic; unsafe as a tactical/football-intelligence claim |
| `ratings.soccer_intelligence` | Public placeholder | None | None | `value=null`; `confidence=0` | Never currently eligible | Zero is a fixed unsupported-state confidence, not observed evidence | `unsupported_without_validated_evidence` | Unsupported placeholder |
| `ratings.tactical_vision` | Public placeholder | None | None | `value=null`; `confidence=0` | Never currently eligible | Same as above | `unsupported_without_validated_evidence` | Unsupported placeholder |
| `ratings.mental_stability` | Public placeholder | None | None | `value=null`; `confidence=0` | Never currently eligible | Same as above | `unsupported_without_validated_evidence` | Unsupported placeholder |
| `ratings.professionalism` | Public placeholder | None | None | `value=null`; `confidence=0` | Never currently eligible | Same as above | `unsupported_without_validated_evidence` | Unsupported placeholder |
| `ratings.growth_potential` | Public placeholder | None | None | `value=null`; `confidence=0` | Never currently eligible | Same as above | `unsupported_without_validated_evidence` | Unsupported placeholder |
| `ratings.market_readiness` | Public placeholder | None | None | `value=null`; `confidence=0` | Never currently eligible | Same as above | `unsupported_without_validated_evidence` | Unsupported placeholder |
| `ratings.scalability` | Public placeholder | None | None | `value=null`; `confidence=0` | Never currently eligible | Same as above | `unsupported_without_validated_evidence` | Unsupported placeholder |
| `overall` | Overall Rating using currently available supported categories | Available PlayerRatingEngine categories | Requires any two available categories. Configured weights are Technical `0.45`, Physical `0.30`, Ball `0.25`; weights are renormalized over whichever categories are available. Value is their weighted sum. | Unitless `0..100`; level bands are `<20 very_low`, `<35 low`, `<50 developing`, `<65 moderate`, `<80 good`, `<90 very_good`, otherwise `excellent` | Any two categories marked `available`; Technical is not mandatory | Mean included-category confidence × `min(1, evidence duration/5)` × `included category count/3`, clamped to `0..1`; evidence adequacy, not calibrated correctness | `insufficient_supported_categories` when fewer than two categories are available | Provisional composite metric; not validated rating |
| `overallConfidence` | Top-level convenience copy of Overall confidence | `player_rating_summary.overall.confidence` | Exact copy; no new transform | Unitless `0..1` | Emitted for current `AVAILABLE` callbacks, including when Overall itself is insufficient and has `value=null` | Same evidence-adequacy score as Overall; not AI certainty or probability | Null for `UNAVAILABLE`; absent from failed callback schema; legacy-compatible payloads may serialize null | Evidence quality |

Formula sources: [`TechnicalScorer`](../../../src/services/scoring/technical.py#L27), [`RuleBasedPhysicalActivityScorer`](../../../src/services/scoring/physical_activity.py#L22), [`PlayerRatingEngine`](../../../src/services/player_rating/engine.py#L26), and [`PlayerRatingConfig`](../../../src/services/player_rating/config.py#L24). Public projection and level/status envelopes are built in [`public_rating_v2`](../../../src/api/public_rating_mapper.py#L33) using [`PublicRatingValue`](../../../src/schemas/public_rating_v2.py#L20).

### Public Game Intelligence component scores

These are nested public scores and therefore require explicit lineage even though this document recommends no Apex promotion yet.

| Component | Formula and inputs | Eligibility/confidence | Semantic boundary |
|---|---|---|---|
| `ball_involvement` | `100 × (0.35 proximity ratio + 0.25 interaction duration/5 + 0.20 count/4 + 0.10 longest/3 + 0.10 interaction confidence)`, with capped normalized inputs | Positive interaction count and complete finite inputs; ball and interaction quality each at least `0.45`. Confidence is the mean of interaction confidence, coverage, ball quality, and interaction quality. | Proximity/interaction consistency, not possession skill |
| `decision_consistency` | Uses Technical value when present, otherwise positive candidate-event count and confidence; applies a loss ratio penalty of up to 35% | Positive event evidence and non-zero technical quality. Confidence is technical quality × positive-event confidence, with overlap reduction. | Candidate-event consistency, not decision quality |
| `spatial_activity_proxy` | `100 × (0.35 movement intensity + 0.25 active time + 0.20 direction + 0.20 normalized direction-change rate)` | Complete movement inputs and movement quality at least `0.55`. Confidence averages movement quality, continuity, and visibility. | Image-space activity, not positioning or spatial awareness |
| `movement_efficiency_proxy` | `100 × (0.35 movement intensity + 0.25 direction + 0.20 active time + 0.20 continuity)` | Complete movement inputs and movement quality at least `0.55`; same evidence-confidence family as spatial proxy. | Image-space motion, not speed, stamina, or efficiency against a football objective |
| `technical_involvement` | Copies Technical value; confidence is Technical confidence × technical-event quality | Finite Technical value/confidence | Adapted candidate-event score, not an independent tactical construct |

The implementation and its own caveats are in [`GameIntelligenceEngine`](../../../src/services/player_rating/game_intelligence.py#L104). The naming remains product-significant even when explanations are cautious.

### Detailed callback scores

These fields are callback-public but lack the canonical rating envelope: no per-field status, reason, confidence, limitations, or version accompanies the number.

| Public field | Actual source and transform | Unit/scale and eligibility | Null reasons | Evidence class |
|---|---|---|---|---|
| `detailed.speed_and_fitness` | `physical.evidence.movement_intensity × 100` | Unitless `0..100`; only if physical status is exactly `provisional_video_based` and movement intensity is finite | Missing/rejected physical evidence or non-finite intensity | Image-space observation transformed into an unsafe fitness-labelled provisional metric |
| `detailed.ball_control_and_individual_skill` | Pooled mean of every controlled/dribble candidate component, less loss penalty, × 100. Unlike the current Technical category, this pools individual events rather than equal-weighting event-type means. | Unitless `0..100`; requires positive finite technical-event quality and at least one controlled/dribble candidate | Missing/non-positive quality or no positive candidate | Provisional candidate-event metric |
| `detailed.passing_and_playmaking` | `100 × mean(confidence)` for accepted, finite, selected-target-attributed pass candidates | Unitless `0..100`; requires arbitration, target ID, accepted target pass candidate | No arbitration/target, no accepted target candidate, non-finite confidence, rejected/ambiguous event, or only other-player events | Event evidence, not passing skill or pass completion |
| `detailed.shooting_and_finishing` | `100 × mean(confidence)` for accepted, finite, selected-target-attributed shot candidates | Unitless `0..100`; analogous to passing | Analogous to passing | Event evidence, not shooting skill, shot outcome, or finishing |
| `detailed.defending_and_duels` | No source or transform | Always null | Unsupported | Unsupported placeholder |
| `detailed.tactical_intelligence_and_teamwork` | No source or transform | Always null | Unsupported | Unsupported placeholder |
| `detailed.positioning_and_off_ball_movement` | No source or transform | Always null | Unsupported | Unsupported placeholder |

Source: [`DetailedRatingEngine.evaluate`](../../../src/services/detailed_rating/engine.py#L36) and its component formulas at [`_ball_control`](../../../src/services/detailed_rating/engine.py#L144). The target-attribution behavior is covered by [`test_other_players_events_do_not_contaminate_target_detailed_ratings`](../../../tests/test_detailed_rating.py#L292), but that protection is not applied to public Game Intelligence or the public event summary.

### Public tracking and event confidences that are not scores

`player.selection_confidence` is the selected track's average detector confidence, copied through `SelectedPlayer.confidence`; it is not identity confidence and not football ability. See [selected-player assembly](../../../src/api/routes.py#L1792) and [public player projection](../../../src/api/public_rating_mapper.py#L118).

Candidate event `confidence` values and event-derived mean confidences are detector/heuristic evidence. They cannot be presented as skill, completion, finishing, or tactical correctness. `quality.tracking`, `quality.movement`, `quality.interaction`, and `quality.technical_events` are diagnostics, not ratings.

## F02 — public score meaning and eligibility

F02 is not satisfied as a safe product contract today:

- Current Technical, Physical Activity, and Ball Activity envelopes have explicit status/reason/confidence fields, while the proposed canonical contract adds the fuller status/reason/confidence/limitations semantics described below; all remain provisional transforms rather than validated ability ratings.
- The detailed callback surface presents bare numbers under ability-like labels. Two are direct event-confidence means and one presents image-space movement intensity as “speed and fitness.”
- `AVAILABLE` is a target-result envelope, not a statement that Overall or every category is numeric. Current callback assembly marks a completed result `AVAILABLE` when a rating summary exists, even if `overall.value` is null.
- The caller-supplied `playerId` is correlation metadata. The visual target is chosen independently; target establishment does not prove that the visual track is the requested person.
- Pass/shot entries from other tracks in the selected interval influence Game Intelligence and public summary/timeline. They do not influence the separately target-filtered detailed pass/shot numbers.

## F12 — state, status, and null consistency

### Complete external callback state matrix

`null`, numeric zero, empty object, and absent field are four different wire states and must remain distinct.

| External case | Callback status | Availability fields | Player / ratings / Overall | Detailed / events / error | Queue/lifecycle consequence |
|---|---|---|---|---|---|
| Legacy success compatibility | Caller-provided legacy string, demonstrated as lowercase `completed` | Alias fields serialize as null under the current success schema when unset | Legacy values preserved; `player=null`; `overallConfidence=null` | Legacy detailed/events preserved; `error=null` | Compatibility-only construction; current normal completed mapper uses uppercase `COMPLETED` and explicit availability |
| Current `AVAILABLE` | `COMPLETED` | `resultAvailability="AVAILABLE"`; `unavailabilityReason=null` | `player` present; ratings object present; Overall object present but its `value` may be null; `overallConfidence` numeric, including legitimate `0.0` | Detailed object present, potentially mixed numeric/null; event timeline present; `error=null` | Queue returns `COMPLETED` |
| Current `UNAVAILABLE` | `COMPLETED` | `resultAvailability="UNAVAILABLE"`; approved reason required | `player=null`; `ratings={}`; `overall=null`; `overallConfidence=null` | All seven detailed values null; `events={}`; `error=null` | Analysis completed but no dominant visual target; queue returns `COMPLETED` |
| Pipeline `FAILED` | `failed` | Fields are absent because `FailedCallbackPayload` does not define them | `ratings={}`; `overall=null`; no `player`; no `overallConfidence` | `detailed` absent; `events={}`; sanitized `error` present | Queue returns `FAILED` |
| No person detected | `no_players_detected` | Success-schema aliases serialize null | `player=null`; `ratings={}`; `overall=null`; `overallConfidence=null` | Detailed object with nulls; `events={}`; `error=null` | Non-completed analysis response, but the processor returns queue `COMPLETED` after callback construction |
| Person detected, no track | `player_detection_completed_tracking_not_available` | Success-schema aliases serialize null | Same as no person | Same as no person | Processor returns queue `COMPLETED` |
| Target not established | `COMPLETED` | `UNAVAILABLE` plus one of `ambiguous_visual_target`, `no_qualifying_visual_target`, or `target_not_established` | Same as current `UNAVAILABLE` | Same as current `UNAVAILABLE` | Rating completion is bypassed; queue returns `COMPLETED` |
| Callback delivery failure | Payload state is unchanged | Unchanged from the attempted payload | Unchanged | Delivery retries are exhausted or URL is rejected; failure is logged, not represented in the callback body | Analysis state remains `COMPLETED` or `FAILED`; no durable delivery state is added here |

Sources: callback construction in [`_callback_payload`](../../../src/api/routes.py#L207), failure construction and processor terminal states in [`create_analysis_job_processor`](../../../src/api/routes.py#L427), retry/serialization behavior in [`CallbackService.send_result`](../../../src/services/callback_service.py#L123), and target bypass in [`_analyze_uploaded`](../../../src/api/routes.py#L675). Transport tests preserve zero versus null and legacy bytes in [`test_callback_transport_alias_contract.py`](../../../tests/services/test_callback_transport_alias_contract.py#L88).

### Current contradictions

1. `CompletedResponse` treats `result_availability=None` as equivalent to `AVAILABLE`, but requires only selected player and scores; it does not require `player_rating_summary`. The callback path later requires the summary. See [`CompletedResponse._validate_result_availability`](../../../src/schemas/analysis.py#L370).
2. `CallbackPayload` `AVAILABLE` validation requires only `COMPLETED`, player present, and no reason. It can accept empty ratings, null Overall, null `overallConfidence`, and all-null detailed fields.
3. `CallbackPayload` `UNAVAILABLE` validation rejects player/Overall/confidence, but does not reject non-empty ratings, summary, events, detailed numbers, or `error`. Runtime assembly is clean, while the schema admits contradictory objects. See [`CallbackPayload._validate_target_result_availability`](../../../src/services/callback_service.py#L56).
4. Non-completed public V2 failure has `reason`, `reason_code`, warnings, and retryability; callback projection discards those fields for no-person/no-track states and leaves `error=null`.
5. `ScoresResponse.game_intelligence` is always an internal `UnsupportedMetric`, while the public mapper independently calculates and publishes a numeric Game Intelligence heuristic. See [`ScoresResponse`](../../../src/schemas/analysis.py#L327), [`FeatureExtractor.scores`](../../../src/services/feature_extractor.py#L74), and [`public_rating_v2`](../../../src/api/public_rating_mapper.py#L33).
6. Public quality objects use fallback zero and fixed `available/good` labels in some places even when the underlying value was absent. Those diagnostics must not be mistaken for score availability.

### Proposed canonical result availability

These definitions are normative for the proposed canonical contract. Current schemas do not yet enforce every stated combination, as documented above.

#### `AVAILABLE`

`AVAILABLE` means Super-7 produced a valid result envelope attributable under the currently accepted target-selection contract.

It does not mean:

- every rating is numeric;
- the target is cryptographically or biometrically verified as the Apex `playerId`;
- every football dimension was observed;
- the scores are scientifically validated ability ratings.

Individual categories retain independent status and may be unavailable inside an `AVAILABLE` result envelope. The Apex-owned `playerId` remains correlation metadata and is not proof that the selected visual track depicts that person. Every canonical v1 `AVAILABLE` result must include this proposed safe, versioned projection:

```json
{
  "targetAttribution": {
    "method": "DOMINANT_VISUAL_TARGET",
    "status": "ESTABLISHED",
    "requestedPlayerIdentityStatus": "NOT_VERIFIED",
    "limitations": [
      "The selected visual target is not verified as the playerId identity"
    ],
    "version": "target-attribution-v1"
  }
}
```

This projection is proposed canonical behavior, not current runtime behavior:

- `method` identifies how the visual target was selected.
- `status` reports whether the visual-analysis target was established.
- `requestedPlayerIdentityStatus="NOT_VERIFIED"` means identity verification was not performed and no identity claim was established. It is not evidence of a proven mismatch.
- `limitations` is public contract material and Apex must not silently discard it.
- `version` identifies the attribution-contract version.

If target selection is established without requested-player identity verification, the canonical state is `resultAvailability="AVAILABLE"`, `targetAttribution.status="ESTABLISHED"`, and `targetAttribution.requestedPlayerIdentityStatus="NOT_VERIFIED"`. Apex may display the correlation `playerId`, but it must not represent the selected target as identity-verified. This is a controlled-pilot boundary, not final identity assurance.

#### `UNAVAILABLE`

Use `UNAVAILABLE` when the minimum target/evidence boundary required to publish an attributable result was not established.

In that state:

- player-attributed rating values must not be published as valid results;
- the unavailability reason must be explicit;
- missing evidence must not become zero.

The proposed canonical projection for that state is:

```json
{
  "targetAttribution": {
    "method": "DOMINANT_VISUAL_TARGET",
    "status": "NOT_ESTABLISHED",
    "requestedPlayerIdentityStatus": "NOT_VERIFIED",
    "limitations": [
      "No publishable visual target was established"
    ],
    "version": "target-attribution-v1"
  }
}
```

The separately required `unavailabilityReason` explains why a publishable target/result was unavailable. Dominant-target selection establishes, at most, a provisional visual analysis target. It is not verified player identity.

A future stronger identity contract requires one or more separately approved mechanisms—for example controlled single-player capture requirements, manual target initialization, an Apex-provided target hint, jersey-number evidence, Re-ID, or human-verifiable identity ground truth. This document does not select or implement any such mechanism.

### Proposed canonical state representation

This matrix freezes canonical v1 behavior. It does not describe the current runtime matrix above.

| Case | Canonical v1 representation |
|---|---|
| Field/category `DROP_FROM_V1` | Absent from the v1 schema and payload |
| Field/category `DEFER` | Absent from the v1 schema and payload |
| Included category with sufficient evidence | Category object present with numeric `value` and an available status |
| Included category with insufficient evidence inside an `AVAILABLE` result | Category object present with `value=null`, explicit non-available status, reason, limitations, evidence-confidence semantics, and version |
| Legitimate measured zero | Category object present with `value=0` and an available status |
| Result `UNAVAILABLE` | `targetAttribution` and `unavailabilityReason` present; player-attributed `ratings` and `overallEvidenceScore` absent |
| Result `FAILED` | Separate sanitized failure envelope; ratings and `overallEvidenceScore` absent; never relabelled `UNAVAILABLE` |
| Empty ratings object | Prohibited in canonical v1 |
| Unsupported placeholder objects | Prohibited in canonical v1 |

Normatively, **absent** means a field is not part of that payload, state, or version. **Null** means an included contract field exists but evidence is insufficient for a numeric value. Numeric **zero** is a legitimate observation and must not be treated as missing. An empty object `{}` is not interchangeable with absent or null. Apex must preserve these distinctions through persistence, API mapping, and UI rendering.

## F18 — safe Overall handling

### Current runtime behavior

Current Overall behavior is deterministic but does not yet implement the complete proposed canonical envelope:

- Technical is not mandatory. Any two available categories qualify.
- Missing categories are correctly excluded and weights renormalized; they are not replaced by zero.
- The nominal `45/30/25` weights therefore do not remain the effective weights when a category is unavailable.
- Category confidence is evidence quality, and Overall confidence compounds evidence quality/duration/coverage. Neither is a calibrated probability of football ability or score correctness.
- Current runtime `level` labels such as `moderate`, `good`, `very_good`, and `excellent` are deterministic bands over provisional numbers, not validated football levels. They are not part of proposed canonical v1 and must not be replaced by Bronze/Silver/Gold.
- Current runtime status is the generic category status `available`; it does not distinguish complete three-category Overall from partial two-category Overall.
- Current public evidence does not expose a canonical missing-category list, compatibility/comparison contract, or the complete partial/complete metadata described below.
- Available Game Intelligence passed to the engine is structurally incompatible with the three-entry weight map and can raise `KeyError`. This is latent because the production call omits the optional parameter.

### Proposed canonical `overallEvidenceScore` policy

`overallEvidenceScore` is **a provisional evidence-weighted composite of currently available supported categories**. It is not an overall football-ability rating and is not scientifically validated.

The base weights remain:

- `technical_event_evidence`: `0.45`
- `physical_activity`: `0.30`
- `ball_involvement`: `0.25`

Technical Event Evidence is not independently mandatory. The proposed canonical behavior is:

| Available core categories | Value behavior | Canonical status | Required envelope evidence |
|---:|---|---|---|
| All three | Calculate the normal `overallEvidenceScore` with effective weights `0.45 / 0.30 / 0.25`. | `COMPLETE` | Included categories, no missing categories, effective weights, evidence-confidence meaning, limitations, and formula version |
| Exactly two | Calculate `overallEvidenceScore` from only the two available categories and renormalize their existing base weights to sum to `1.0`. | `PARTIAL` | Included categories, missing category, renormalized effective weights, evidence-confidence meaning, limitations, and formula version |
| Fewer than two | `value=null`; do not calculate `overallEvidenceScore`. | `INSUFFICIENT_EVIDENCE` | Missing categories, reason, limitations, and formula version |

A missing category must never become zero, and a partial `overallEvidenceScore` must never be represented as complete. Values may be compared only when formula version, included-category composition, and evidence contract are compatible.

This is a proposed Super-7 contract direction, not a claim that the current runtime implements its name, status, or metadata envelope. A future validated contract would additionally require labelled football ground truth, calibration, external/generalization evidence, and explicit promotion approval before making a stronger ability claim.

`overallEvidenceScore` remains part of the proposed Super-7 canonical contract, but external publication is blocked until partial/complete semantics, source-status propagation, comparison metadata, canonical naming, and RED contracts are implemented.

### Proposed canonical confidence semantics

Current confidence represents evidence quality or support strength. It is neither identity confidence nor a calibrated probability that a rating equals true football ability.

- It is not the probability that the selected visual track is the requested `playerId`.
- It is not the probability that the player identity is correct.
- It is not a calibrated probability that the score equals true football ability.
- `aiConfidence` is not an acceptable canonical name.
- The proposed canonical concept is `evidenceConfidence`, or an equivalently explicit evidence-quality name.

Current wire fields remain `confidence` inside rating objects and `overallConfidence` at the callback convenience layer. This documentation decision does not rename either runtime field. The alias-to-canonical migration requires a versioned contract and interoperability acknowledgement.

## APX-15 meeting decision package

### Canonical contract ownership

| Responsibility | Owner |
|---|---|
| Score meaning and formula semantics | Super-7 |
| Evidence and availability gates | Super-7 |
| Canonical field names and result envelope | Super-7 |
| `null`, zero, absent, partial, and unavailable semantics | Super-7 |
| Scientific-validation and promotion gates | Super-7 with qualified football/ML validation |
| Apex storage, persistence mapping, and UI presentation | Apex |
| Version compatibility and interoperability acknowledgement | Shared integration responsibility |

Apex may map the canonical contract into its database and UI, but it must not silently rename a provisional metric into a stronger football claim, flatten an object while discarding status or limitations, convert `null` to zero, treat absent as unavailable, treat tracking confidence as identity confidence, or alter category or composite formulas. If Apex cannot consume the canonical versioned object without losing meaning, that is an integration/cutover blocker; Super-7 semantics must not be weakened to fit the current Apex persistence shape.

The classifications below describe current correspondence and evidence readiness. The decision column records the proposed Super-7 v1 direction pending human review. Semantic inclusion is separate from implementation, scientific, and integration readiness. Current Apex or runtime names do not become canonical merely for compatibility.

### Proposed canonical v1 field-decision table

| Proposed concept | Current source/name | Semantic classification | Gate | Final proposed v1 decision | Meaning/boundary | Decision owner |
|---|---|---|---|---|---|---|
| `overallEvidenceScore` | Current `overall`; Apex candidate `super7Score` | `PARTIAL_MATCH`, `SEMANTIC_RENAME_REQUIRED`, `TARGET_SELECTION_GATED`, `EVIDENCE_GATED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Established visual target plus at least two supported categories | `INCLUDE_WITH_CANONICAL_NAME`; implementation and validation gated | Provisional evidence-weighted composite, never a scalar ability claim | Super-7 |
| `ratings.technical_event_evidence` | Current `ratings.technical_skill` | `PARTIAL_MATCH`, `SEMANTIC_RENAME_REQUIRED`, `TARGET_SELECTION_GATED`, `EVIDENCE_GATED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Established visual target, source-status propagation, and controlled/dribble candidate evidence | `INCLUDE_WITH_CANONICAL_NAME`; implementation and validation gated | Candidate-event transform, not general technical skill | Super-7 with qualified football/ML validation |
| `ratings.physical_activity` | Same current runtime name | `PARTIAL_MATCH`, `TARGET_SELECTION_GATED`, `EVIDENCE_GATED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Established visual target and movement evidence gate | `INCLUDE_WITH_CANONICAL_NAME`; movement/activity only | Observed movement/activity, not fitness | Super-7 with qualified football/ML validation |
| `ratings.ball_involvement` | Same current runtime name | `PARTIAL_MATCH`, `TARGET_SELECTION_GATED`, `EVIDENCE_GATED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Established visual target and interaction evidence gate | `INCLUDE_WITH_CANONICAL_NAME`; interaction only | Observed interaction evidence, not possession or ability | Super-7 with qualified football/ML validation |
| `evidenceConfidence` | Nested `confidence`; top-level `overallConfidence`; Apex candidate `aiConfidence` | `PARTIAL_MATCH`, `SEMANTIC_RENAME_REQUIRED`, `EVIDENCE_GATED` | Same evidence gate as the associated object | `INCLUDE_WITH_CANONICAL_NAME`; evidence quality only | Support strength, not identity, ability, or calibrated correctness probability | Super-7 |
| `targetAttribution` | Internal dominant-target evidence has no equivalent safe current public object | `VISUAL_ATTRIBUTION_GATED`, `SEMANTIC_RENAME_REQUIRED` | Dominant visual target evaluation | `INCLUDE_WITH_CANONICAL_NAME`; safe limitation-bearing projection | Exposes method/status/identity-not-verified/limitations/version without raw evidence | Super-7 |
| Current `level` / `super7Level` | Current deterministic `overall.level`; Apex level candidate | `SEMANTIC_RENAME_REQUIRED`, `SCIENTIFIC_VALIDATION_REQUIRED` | No validated level evidence | `DROP_FROM_V1` | Current bands and Bronze/Silver/Gold are excluded | Super-7 |
| `game_intelligence` | Current public heuristic object | `PARTIAL_MATCH`, `SEMANTIC_RENAME_REQUIRED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Attribution and semantic defects unresolved | `DEFER` | Non-GSR heuristic with contamination and consistency defects | Super-7 with qualified football/ML validation |
| Passing | Current detailed confidence transform | `PARTIAL_MATCH`, `SEMANTIC_RENAME_REQUIRED`, `EVIDENCE_GATED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Accepted finite event attributed to selected track | `DEFER` as future event evidence | Not passing ability or completion | Super-7 with qualified football/ML validation |
| Shooting | Current detailed confidence transform | `PARTIAL_MATCH`, `SEMANTIC_RENAME_REQUIRED`, `EVIDENCE_GATED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Accepted finite event attributed to selected track | `DEFER` as future event evidence | Not finishing ability or outcome | Super-7 with qualified football/ML validation |
| `speed_and_fitness` | Current detailed image-motion transform | `UNSUPPORTED`, `SEMANTIC_RENAME_REQUIRED`, `SCIENTIFIC_VALIDATION_REQUIRED` | No metric geometry | `DROP_FROM_V1` | Pixel motion is not speed or fitness | Super-7 |
| `selection_confidence` | Current tracking/detection confidence | `VISUAL_ATTRIBUTION_GATED`, `SEMANTIC_RENAME_REQUIRED` | Selected visual track | `DEFER` | Not identity confidence | Super-7 |
| Agility | None | `UNSUPPORTED`, `SCIENTIFIC_VALIDATION_REQUIRED` | No eligible evidence | `DROP_FROM_V1` | Direction-change proxies do not establish agility | Super-7 with qualified football/ML validation |
| Defense | Current null placeholder | `UNSUPPORTED`, `SCIENTIFIC_VALIDATION_REQUIRED` | No eligible evidence | `DROP_FROM_V1` | No current evidence source or formula | Super-7 with qualified football/ML validation |
| Stamina | None | `UNSUPPORTED`, `SCIENTIFIC_VALIDATION_REQUIRED` | No eligible evidence | `DROP_FROM_V1` | Short-video activity is not stamina | Super-7 with qualified football/ML validation |
| Standalone dribbling rating | No standalone current rating | `UNSUPPORTED`, `EVIDENCE_GATED`, `SCIENTIFIC_VALIDATION_REQUIRED` | Candidate-event evidence only | `DROP_FROM_V1` | Would overstate the supported construct | Super-7 with qualified football/ML validation |
| Raw target evidence | Internal dominant-target evidence | `INTERNAL_ONLY`, `VISUAL_ATTRIBUTION_GATED` | Visual dominance only; never identity verification | Internal only | Never expose the raw internal object in canonical v1 | Super-7 |
| Internal rating summary | Internal `PlayerRatingSummary` | `INTERNAL_ONLY` | Category and target gates | Internal only | No public field unless separately contracted | Super-7 |

The canonical v1 category object is not optional integration decoration. It contains only `value`, `evidenceConfidence`, `status`, `explanation`, `reason`, `limitations`, and `version`. The canonical `overallEvidenceScore` object additionally contains `includedCategories`, `missingCategories`, `effectiveWeights`, and `formulaVersion`. It does not contain `level`. Apex flattening to a scalar or dropping state/limitations is an integration blocker, not an alternative semantic mapping.

Current runtime `level` remains documented only as current behavior. A future bounded band would require a separate decision and validation contract and, if pursued, should use a non-ability name such as `provisionalValueBand`; it is not included in canonical v1.

### Proposed versioned field migration

| Current runtime field | Proposed canonical v1 concept |
|---|---|
| `ratings.technical_skill` | `ratings.technical_event_evidence` |
| `overall` | `overallEvidenceScore` |
| Nested `confidence` / top-level `overallConfidence` | `evidenceConfidence` within the associated versioned object |
| Current `level` | No canonical v1 field |

Recommended external JSON aliases are `technicalEventEvidence`, `overallEvidenceScore`, and `evidenceConfidence`. This is a proposed versioned rename, not an in-place runtime mutation.

The proposed canonical meanings are deliberately narrow:

- `technical_event_evidence` is a provisional transform of selected-target controlled-movement, dribble, and ball-loss candidate evidence; it is not a validated measure of general technical skill.
- `overallEvidenceScore` is a provisional evidence-weighted composite of available supported categories; it is not an overall football-ability rating.
- `evidenceConfidence` is evidence quality/support strength, not identity probability, ability probability, or calibrated correctness probability.
- `physical_activity` remains observed movement/activity evidence, not fitness.
- `ball_involvement` remains observed interaction evidence, not possession or ability.

### Inclusion versus readiness

A proposed semantic decision does not make the current implementation, scientific claim, or integration cutover ready.

| Included canonical field/concept | Semantic decision | Implementation readiness | Scientific readiness | Integration readiness |
|---|---|---|---|---|
| `overallEvidenceScore` | Included as a provisional evidence-weighted composite; Technical Event Evidence is not independently mandatory. | Not ready: implement canonical naming, `COMPLETE`/`PARTIAL`/`INSUFFICIENT_EVIDENCE`, composition metadata, comparison metadata, source-status propagation, and RED contracts. | Provisional composite only; not validated football ability. | Blocked until Apex acknowledges and preserves the versioned object without scalar flattening. |
| `technical_event_evidence` | Included under the evidence-bounded canonical name and full category envelope. | Not ready: current `technical_skill` source status can be discarded and the versioned rename is absent. | Candidate-event provisional metric; stronger ability claim requires labelled validation. | Blocked until Apex preserves the complete object, name, and limitations. |
| `physical_activity` | Included as movement/activity evidence, explicitly not fitness. | Not ready: Physical source status can currently be discarded. | Narrow observational metric only; fitness/ability claims are not validated. | Blocked until Apex preserves the complete object and “not fitness” limitation. |
| `ball_involvement` | Included as interaction evidence, explicitly not possession. | Not ready for cutover: canonical versioning/envelope and callback consistency work remain. | Provisional interaction metric; possession/ability claims are not validated. | Blocked until Apex preserves the complete object and “not possession” limitation. |
| `evidenceConfidence` | Included as the canonical evidence-quality concept; `aiConfidence` is rejected. | Not ready: current runtime fields remain `confidence` and `overallConfidence`; versioned migration is required. | Evidence support only; probability calibration has not been established. | Blocked until Apex consumes the explicit evidence-quality name without identity/ability interpretation. |
| `targetAttribution` | Included as the safe public visual-attribution projection. | Not ready: current runtime has no versioned projection or state-specific enforcement. | Dominant-target selection does not verify requested-player identity; identity assurance remains unvalidated. | Blocked until Apex retains limitations and displays `NOT_VERIFIED` without strengthening the claim. |

### Verified current aliases and Sprint 2 canonical identities

Internal Python names are not automatically external Apex names. The current API mixes queued-response camel case, callback snake case, and selected callback aliases. The Sprint 2 durable contract is a proposed future versioned surface, not implemented current behavior.

| Meaning | Python/internal field | Current serialized JSON field | Current owner | Sprint 2 proposed field | Migration rule |
|---|---|---|---|---|---|
| Request/job identity | `AnalysisJob.analysis_id`; callback `CallbackPayload.request_id` | Queued response `analysisId`; callback `request_id` | Super-7 | `jobId` | Add only in the versioned durable contract. `jobId` becomes the canonical logical-job identity; do not relabel current callback `request_id` in place. |
| Analysis identity | `AnalyzeQueuedResponse.analysis_id`; `CompletedResponse.analysis_id` | Queued response `analysisId`; public projection/callback currently also uses `request_id` | Super-7 | `analysisId` | During the approved migration period, emit `analysisId == jobId`; it is an alias, never a second identity. Remove reliance only after Apex confirms migration. |
| Video identity | `AnalyzeRequest.video_id`; `AnalysisJob.video_id`; callback `CallbackPayload.video_id` | Request/queued response `videoId`; callback `video_id` | Apex | `videoId` | Preserve Apex's value. A versioned callback changes the current callback spelling; do not claim the alias exists today. |
| Player identity | `AnalyzeRequest.player_id`; `AnalysisJob.player_id`; callback `CallbackPayload.player_id` | Request/queued response `playerId`; callback `player_id` | Apex | `playerId` | Preserve as Apex correlation identity. It does not identify or verify a visual track. |
| Callback-event identity | Not implemented | Absent | Future Super-7 ownership | `callbackEventId` | Create after recording a terminal result; persist and reuse across delivery retries/redrives. It identifies delivery of one terminal event, not the analysis. |
| Result availability | `result_availability` | `resultAvailability` | Super-7 | `resultAvailability` | Preserve enum meaning and canonical alias in the versioned result envelope; do not replace it with score presence or a numeric default. |
| Unavailability reason | `unavailability_reason` | `unavailabilityReason` | Super-7 | `unavailabilityReason` | Preserve approved reason enum and require it only with `UNAVAILABLE`; never infer it from null scores. |

Current aliases are defined in [`AnalyzeRequest` and `AnalyzeQueuedResponse`](../../../src/schemas/analysis.py#L17), [`CallbackPayload`](../../../src/services/callback_service.py#L33), and the transport call that serializes with aliases in [`CallbackService.send_result`](../../../src/services/callback_service.py#L123). The current callback alias assertions are in [`test_available_callback_uses_canonical_aliases_in_transport_bytes`](../../../tests/services/test_callback_transport_alias_contract.py#L49). The future identity contract is recorded in [Analysis Job Contract V1](../../contracts/analysis-job-contract-v1.md#L1) and [ADR-004](../../decisions/ADR-004-analysis-job-lifecycle-and-idempotency.md#L15).

`jobId` is the proposed canonical Super-7 job identity. If the approved migration contract remains unchanged, `analysisId` equals `jobId` temporarily. `videoId` and `playerId` remain Apex-owned correlation identities. `callbackEventId` is the proposed durable callback-delivery identity, not an analysis identity. None of `jobId`, `callbackEventId`, durable persistence, or an outbox is implemented in the current runtime; these belong to future Slice 2+/outbox work.

### Minimal proposed v1 boundary for joint review

This boundary distinguishes the current wire from the proposed versioned contract; it does not claim implementation readiness:

- Future V1 identity fields are `jobId`, temporary equal alias `analysisId`, `videoId`, `playerId`, and `callbackEventId`, subject to the shared contract and Slice 2+/outbox implementation. They must not be described as present today.
- Preserve `resultAvailability` and `unavailabilityReason` with their current meanings.
- Include `ratings.technical_event_evidence`, `ratings.physical_activity`, and `ratings.ball_involvement` under the proposed canonical names and complete category envelopes, subject to their implementation gates. Current `ratings.technical_skill` remains only a documented runtime source name.
- Include `overallEvidenceScore` with the proposed complete/partial/insufficient semantics. External publication remains blocked until the envelope, source-status propagation, comparison metadata, versioning, canonical rename, and RED contracts are implemented. Never flatten it to `super7Score`, relabel it as overall ability, or substitute zero.
- Use `evidenceConfidence`, or an equivalently explicit evidence-quality name, as the canonical concept. Preserve current `confidence`/`overallConfidence` only as documented current-wire fields until a versioned migration is implemented; do not expose `aiConfidence`.
- Include the safe `targetAttribution` projection in every `AVAILABLE` and `UNAVAILABLE` canonical result; keep raw target evidence internal and keep its public limitations intact.
- Exclude current `level` and `super7Level`. Do not replace them with Bronze/Silver/Gold or any other ability band in v1.
- `player.selection_confidence` v1 disposition: `DEFER`. It is tracking/detection confidence for the selected visual track—not identity confidence, not the probability that the track is the requested `playerId`, and not football-ability confidence. It may be reconsidered only under an explicit canonical name and limitation.
- Defer `game_intelligence`, detailed ability-like fields, and target-framed `events`/event-count `summary` until their respective semantic, validation, and attribution blockers are resolved.
- Dropped, deferred, and unsupported placeholder fields are absent from the canonical v1 schema and payload. Included-but-insufficient categories remain present with `value=null` and explicit state; legitimate measured zero remains numeric `0`; empty ratings objects are prohibited.

For `UNAVAILABLE`, the minimal payload is canonical correlation/lifecycle fields, `resultAvailability="UNAVAILABLE"`, safe `targetAttribution`, and one approved `unavailabilityReason`; player-attributed ratings and `overallEvidenceScore` are absent. For `FAILED`, use the separate sanitized failure envelope with ratings and composite absent; do not relabel it `UNAVAILABLE`.

### Risk/action matrix

This table assigns future RED-contract work; it does not authorize fixes in APX-15.

| Finding | Current impact | APX-15 impact | Severity | Owner | Required next task | Blocks v1 mapping? |
|---|---|---|---|---|---|---|
| Dominant visual target does not verify `playerId` identity | A result can be attributed to the dominant visual track without proof it is the requested real-world player. | Any identity-strengthened presentation would be false; controlled-pilot attribution must remain explicit. | **P0 / Critical** | Super-7 attribution contract; Apex presentation | Implement safe `targetAttribution`; retain `NOT_VERIFIED`; define and validate a separate future identity-assurance contract. | Yes for any verified-player claim |
| Other-player pass/shot contamination in Game Intelligence, summary, and timeline | Target-facing public outputs can incorporate interval events attributed to another track. | `game_intelligence`, `summary`, and `events` are unsafe as target-player mappings. | High | Super-7 public mapper/event attribution | RED contract: selected-target attribution; keep Game Intelligence and target-framed event outputs deferred meanwhile. | Yes for those fields; no if deferred |
| Rejected/unavailable Technical evidence can become `available` | Source status is discarded when numeric value/confidence exists. | Current `technical_skill`, and therefore proposed `technical_event_evidence`, can contradict the evidence gate. | High | Super-7 scoring | RED contract and implementation: preserve/reject source status according to the canonical evidence gate. | Yes for Technical Event Evidence publication |
| Rejected/non-provisional Physical evidence can become `available` | Source status is discarded when numeric value/confidence/evidence exists. | `physical_activity` can contradict its evidence gate. | High | Super-7 scoring | RED contract: require the approved physical source status before category availability. | Yes for Physical publication |
| Optional Game Intelligence can cause latent Overall `KeyError` | Passing an available optional category reaches a missing weight-map key. | A future accidental admission could fail callback production even though Game Intelligence is deferred from canonical v1. | High | Super-7 scoring | RED contract: deferred/unknown categories cannot enter the core Overall weight map or crash calculation. | Yes for any future GI admission; no while excluded |
| Current Overall lacks canonical partial/complete semantics | Two categories can produce a value, but runtime status does not distinguish partial from complete and lacks required composition/comparison metadata. | `overallEvidenceScore` cannot yet be published safely or compared compatibly. | High | Super-7 scoring and contract | RED contracts and implementation for `COMPLETE`, `PARTIAL`, `INSUFFICIENT_EVIDENCE`, included/missing categories, effective weights, confidence meaning, limitations, and formula version. | Yes for composite publication |
| Callback schemas admit contradictory `AVAILABLE`/`UNAVAILABLE` objects | Invalid combinations can pass model validation even though normal assembly is cleaner. | Apex cannot rely on schema validity alone for state consistency. | High | Super-7 callback contract | RED contract: reject contradictory ratings/summary/events/detailed/error combinations under the proposed state semantics. | Yes for callback publication/cutover |
| Internal/public Game Intelligence inconsistency | Internal `ScoresResponse` says unsupported while public mapper can emit a heuristic value. | Two Super-7 surfaces disagree about support. | High | Super-7 scoring and contract | RED contract: canonical v1 keeps Game Intelligence deferred and prevents contradictory public promotion. | Yes for Game Intelligence; no while deferred |
| No-person/no-track reason loss | Public V2 has reason fields, but callback projection emits empty score material with null error/availability. | Apex receives a status but loses the richer machine-readable reason envelope. | Medium | Super-7 callback contract | RED contract: preserve canonical non-completed reasons; shared interoperability acknowledgement is required for cutover. | Yes for failure-state mapping |
| Ability-like detailed names expose event or movement confidence | Event confidence appears as passing/shooting ability and image-space motion as speed/fitness. | Direct Apex mapping would overclaim skill and physical ability. | Critical | Super-7 semantics with qualified football/ML validation | Enforce the proposed drop/defer decisions and add RED contracts prohibiting evidence-to-skill relabelling. | Yes for detailed fields; no when dropped/deferred |

## Proposed canonical semantic decisions

- Super-7 owns canonical score meaning, formulas, evidence/availability gates, field names, result envelopes, missingness semantics, and scientific promotion gates.
- `overallEvidenceScore` is included as a provisional evidence-weighted composite. Technical Event Evidence is not independently mandatory: three available core categories produce `COMPLETE`, exactly two produce `PARTIAL` with renormalized base weights, and fewer than two produce `INSUFFICIENT_EVIDENCE` with `value=null`.
- Base composite weights remain Technical Event Evidence `0.45`, Physical Activity `0.30`, and Ball Involvement `0.25`; missing categories never become zero.
- `technical_event_evidence`, `physical_activity`, and `ball_involvement` are included under evidence-bounded canonical names and full envelopes, subject to implementation gates and explicit limitations.
- `targetAttribution` is included as the safe limitation-bearing public projection; raw target evidence remains internal.
- Current `level` and `super7Level` are dropped from v1. No replacement band is included.
- `game_intelligence` is deferred from canonical v1.
- Pass and shot outputs are deferred future event evidence, not passing or finishing ability.
- `speed_and_fitness`, `super7Level`, agility, defense, stamina, and standalone dribbling are dropped from v1 under those claims.
- `selection_confidence` is deferred and is not identity confidence.
- Internal rating summary remains internal unless a separate canonical public contract is approved.
- Canonical confidence means evidence quality/support strength under an explicit name such as `evidenceConfidence`; it is not calibrated identity or football-ability probability.
- `AVAILABLE` and `UNAVAILABLE` use the normative result-envelope meanings defined above; category availability remains independent inside an available result, and both states carry safe `targetAttribution` without asserting identity verification.
- Dropped/deferred fields are absent; included-but-insufficient values are null inside explicit objects; legitimate zero stays zero; empty ratings and unsupported placeholders are prohibited.

## Remaining product/attribution gates

- Controlled-pilot capture assumptions must be written.
- Apex must not represent the correlation `playerId` or dominant visual target as visually verified identity.
- A future identity-assurance mechanism requires a separate approved contract.
- Promotion from dominant visual target to verified-player attribution requires evidence and tests.

## Remaining implementation gates

- RED contracts for every blocker in the risk/action matrix.
- Technical and Physical source-status propagation.
- Safe versioned `targetAttribution` projection.
- Current-to-canonical field migration, including `technical_event_evidence`, `overallEvidenceScore`, `evidenceConfidence`, and durable identities.
- Removal of `level` from the public canonical object.
- The canonical `overallEvidenceScore` partial/complete/insufficient envelope.
- Included/missing-category composition and comparison metadata.
- Exact state-specific absence/null/zero enforcement and rejection of empty ratings objects.
- Callback-schema consistency for `AVAILABLE`, `UNAVAILABLE`, failure, null, empty, and absent states.
- Selected-target event attribution safety.
- RED contracts for canonical naming and public attribution limitations.
- Formula, rating-envelope, callback, and identity versioning.
- Apex interoperability acknowledgement that the canonical objects and semantics will be preserved through storage and UI presentation.

Apex internal pending tests do not block Super-7 local contract or RED/GREEN implementation work. They may block shared interoperability validation or production cutover only.

## Remaining scientific gates

- Labelled validation data with documented provenance and leakage controls.
- Qualified football-expert ground truth and inter-rater agreement.
- Requested-player identity ground truth and wrong-player attribution measurement.
- Abstention behavior when target identity or attribution is uncertain.
- Metric validation and calibration for any stronger football or probability claim.
- GSR shadow-mode benchmark evidence where GSR is being considered.
- Pre-registered promotion criteria, subgroup checks, external/generalization evidence, and Super-7 promotion approval with qualified football/ML validation.

## Remaining integration gates

- Apex must preserve the versioned category, composite, and `targetAttribution` objects without scalar flattening or limitation loss.
- Apex must render `requestedPlayerIdentityStatus="NOT_VERIFIED"` without implying either verified identity or a proven mismatch.
- Persistence, API mapping, and UI rendering must preserve absent, null, numeric zero, and prohibited-empty-object distinctions.
- Shared interoperability validation must confirm the current-to-canonical migration names and state-specific field presence.
- Production cutover remains blocked until version compatibility and the complete public envelope are acknowledged.

## Proposed red tests

These tests are proposals only; none were created or run.

1. Construct `TechnicalScoreResult(value=70, confidence=.8, status="unavailable", ...)` and assert the rating adapter does not emit category status `available`.
2. Construct a rejected/non-provisional physical result carrying numeric value/confidence/evidence and assert it cannot become an available public category.
3. Pass an available optional Game Intelligence result to the rating engine and assert it neither enters canonical v1 `overallEvidenceScore` nor causes `KeyError` while the category is deferred.
4. With exactly two core categories, assert `overallEvidenceScore` is `PARTIAL`, renormalizes only their base weights, and exposes included/missing categories, effective weights, evidence-confidence meaning, limitations, and formula version.
5. With all three core categories, assert `overallEvidenceScore` is `COMPLETE` with effective weights `0.45/0.30/0.25`; with fewer than two, assert `value=null` and `INSUFFICIENT_EVIDENCE`.
6. Assert a partial `overallEvidenceScore` cannot serialize as complete and incompatible formula/composition/evidence contracts cannot be treated as directly comparable.
7. Build an `AVAILABLE` result with independently unavailable included categories and assert each is present with `value=null`, explicit non-available status, reason, limitations, evidence-confidence semantics, and version—without zero filling.
8. Build an `UNAVAILABLE` result with player-attributed ratings, `overallEvidenceScore`, summary, events, or numeric detailed fields and assert rejection.
9. Verify no-person/no-track callback reason semantics are retained rather than silently discarded under the canonical failure-state contract.
10. Add another player's accepted pass/shot in the selected interval and assert it cannot change a target-attributed Game Intelligence, event summary, or target event timeline.
11. Assert `speed_and_fitness` is absent from canonical v1 under that name and meaning.
12. Assert pass/shot detector confidence is never serialized under a skill or outcome name.
13. Assert numeric zero, explicit null, prohibited empty object, and absent field remain distinguishable for every external state.
14. Assert target establishment and `selection_confidence` cannot be described or serialized as identity verification or ability confidence.
15. Assert legacy identity-gate classification terminology is absent for dominant visual target selection; use `TARGET_SELECTION_GATED` or `VISUAL_ATTRIBUTION_GATED`.
16. Assert every canonical `AVAILABLE` result contains the versioned `targetAttribution` projection.
17. Assert `requestedPlayerIdentityStatus="NOT_VERIFIED"` cannot serialize or render as verified identity and is not interpreted as a proven mismatch.
18. Assert canonical `UNAVAILABLE` contains `targetAttribution` with `status="NOT_ESTABLISHED"` and `unavailabilityReason`, while player-attributed ratings and `overallEvidenceScore` are absent.
19. Assert current runtime `level` is absent from canonical v1 category and composite objects.
20. Assert `super7Level` is absent from canonical v1.
21. Assert current `ratings.technical_skill` maps only to proposed `ratings.technical_event_evidence` / `technicalEventEvidence`, never to a validated skill claim.
22. Assert current `overall` maps only to `overallEvidenceScore`, never to generic player ability or scalar `super7Score`.
23. Assert every `DROP_FROM_V1` and `DEFER` field is absent from the canonical schema and payload.
24. Assert included-but-insufficient categories are present with `value=null` and the complete explicit non-available state.
25. Assert a legitimate measured zero remains numeric `0` with an available status.
26. Assert empty ratings objects and unsupported placeholder objects are rejected.
27. Assert `FAILED` and `UNAVAILABLE` use separate envelopes and cannot be relabelled as each other.
28. Assert Apex-compatible serialization cannot remove `limitations`, flatten canonical category/composite objects, or strengthen `targetAttribution` into an identity claim.

Existing tests already establish useful boundaries: [zero versus insufficient evidence](../../../tests/test_public_contract_stability.py#L39), [two-category Overall behavior](../../../tests/test_player_rating_engine.py#L59), [detailed null versus zero](../../../tests/test_detailed_rating.py#L229), [target-unavailable scoring bypass](../../../tests/api/test_dominant_target_route_contract.py#L167), and [callback availability aliases](../../../tests/api/test_callback_target_availability_contract.py#L69).

## Appendix — Future scientific-validation and GSR promotion gate

GSR is not implemented in the current runtime, and no current Super-7 score is GSR-derived. It is a future discovery/benchmark candidate only. GSR does not by itself prove requested-player identity or validate a football rating. No GSR-derived field may enter `overallEvidenceScore` or the Apex payload before benchmark evidence exists, shadow-mode and labelled-validation gates pass, and the resulting promotion receives approval.

### Labelled-validation plan for future GSR-derived features

GSR-derived features must enter as internal research evidence first, not as public scores.

1. **Freeze the intended construct.** Define whether each label represents an observable event, tactical choice, off-ball behavior, physiological response, or expert holistic rating. Do not use “Game Intelligence” as an undefined omnibus label.
2. **Define the unit of annotation.** Specify player identity, time window, phase of play, role/position, score state, possession context, and whether labels are event-, sequence-, match-, or player-level.
3. **Establish identity ground truth.** Link the requested player to the visual track with human-verifiable identity labels. Dominant visual target selection is not sufficient.
4. **Create independent labels.** Use trained football annotators with a written rubric; measure inter-rater agreement; adjudicate disagreements; keep detector outputs hidden from annotators to avoid leakage.
5. **Prevent leakage.** Split by player, match, team, competition, venue, and capture source as appropriate. No adjacent clips from the same sequence across train/validation/test.
6. **Represent hard negatives and missing context.** Include occlusion, cuts, camera motion, crowded scenes, off-ball periods, other-player events, ambiguous possession, and clips where no inference is justified.
7. **Pre-register gates.** Before examining the held-out test set, define minimum discrimination/error, probability calibration if confidence is claimed, subgroup performance, abstention behavior, and acceptable false-positive/false-attribution rates.
8. **Compare baselines and ablations.** Demonstrate incremental value over current video-only proxies and measure each GSR feature's contribution. A feature is not promoted merely because it correlates in-sample.
9. **Run shadow mode.** Record versioned internal features and predictions without exposing them to Apex or users; audit drift, missingness, identity errors, and cross-domain performance.
10. **Obtain bounded sign-off.** Super-7 with qualified football/ML validation approves the construct, evidence, and promotion claim; Apex acknowledges versioned interoperability and controls only its storage/UI mapping and production cutover.

### GSR feature promotion checklist

A GSR-derived feature remains internal until every applicable item is complete:

- [ ] The public construct and forbidden interpretations are written.
- [ ] The raw GSR source, units, synchronization, preprocessing, and missingness are documented.
- [ ] Consent, privacy, retention, and acceptable-use review is complete.
- [ ] Requested-player identity is ground-truthed independently of visual dominance.
- [ ] The annotation rubric, annotator qualifications, agreement, and adjudication are recorded.
- [ ] Dataset provenance and player/match/team/capture-source split rules prevent leakage.
- [ ] Sample-size/power rationale and subgroup coverage are documented.
- [ ] A locked held-out test set exists and evaluation gates were pre-registered.
- [ ] Calibration is measured if any field is described as confidence or probability.
- [ ] Abstention/null behavior is evaluated; missing GSR never becomes zero.
- [ ] Other-player and wrong-track contamination tests pass.
- [ ] Video-only versus GSR-added ablations show reproducible incremental value.
- [ ] External or prospective validation meets the approved gate.
- [ ] Failure modes, limitations, drift thresholds, and rollback criteria are documented.
- [ ] The feature is versioned from raw evidence through public projection.
- [ ] The `overallEvidenceScore` inclusion decision and any weight change have a separate approved decision record.
- [ ] Super-7 with qualified football/ML validation approves semantic promotion; Apex interoperability/cutover acknowledgement is recorded separately.

## Non-negotiable prohibitions

- Pixels or pixels/second must not be presented as metres, metres/second, or physical distance/speed.
- Tracking or detector confidence must not be presented as identity certainty or player ability.
- Event confidence must not be presented as passing, shooting, finishing, dribbling, or other skill.
- Missing or unsupported values must not be replaced by zero.
- Dropped, deferred, and unsupported fields must be absent from canonical v1; unsupported placeholder and empty ratings objects are prohibited.
- Included-but-insufficient categories must use explicit null state, while legitimate measured zero must remain numeric zero.
- Unvalidated heuristics must not be marketed as tactical intelligence, decision quality, spatial awareness, fitness, or stamina.
- `playerId` must not be treated as proof that the dominant visual track depicts that player.
- `requestedPlayerIdentityStatus="NOT_VERIFIED"` must not be presented as verified identity or as a proven mismatch.
- Raw internal target evidence must not be exposed in place of the safe `targetAttribution` projection, and Apex must not drop its limitations.
- Current runtime `level`, `super7Level`, and replacement ability bands must not be exposed through canonical v1.
- Current `technical_skill` and `overall` must not be presented as proposed canonical names; canonical v1 uses `technicalEventEvidence` and `overallEvidenceScore`.
- `FAILED`, `UNAVAILABLE`, insufficient evidence, and numeric zero must remain distinct external states.

## Review outcome required

Human review accepted this canonical direction. The acceptance covers the proposed semantics, ownership boundary, field dispositions, attribution limitations, and state rules only. Implementation, scientific validation, requested-player identity assurance, Apex interoperability, and production cutover remain pending. This acceptance does not claim runtime readiness and does not authorize a schema change, callback change, score-formula change, migration, deployment, GSR feature admission, or production activation.
