# Super 7 scoring and response discovery

## 1. Executive Summary

**Verified.** POST /analyze is asynchronous. It returns HTTP 202 with only analysisId, videoId, playerId, and status "queued"; it returns no scores. Final data is delivered to the callback. Evidence: src/api/routes.py:105-152; src/schemas/analysis.py:30-38.

**Verified.** The active callback has root overall and a ratings map. Existing score keys are technical_skill, physical_activity, ball_involvement, and game_intelligence. No top-level technical or physical numeric field exists. Evidence: src/api/routes.py:157-173; src/api/public_rating_mapper.py:50-57.

**Verified.** Physical activity explicitly is not fitness; technical scoring is controlled-movement/dribble candidate evidence rather than the full individual-skill concept; game intelligence explicitly is not tactical assessment. Evidence: src/services/scoring/physical_activity.py:122-128; src/services/player_rating/engine.py:75-105; src/services/player_rating/game_intelligence.py:91-137,256-300.

**Recommendation (Inferred, high confidence).** Preserve the callback exactly and, after Apex confirms unknown-key tolerance, add one nullable root detailed object. Do not move fields or introduce schema_version in the same change. Initially every detailed value should be null.

## 2. Scope and Safety Confirmation

**Verified.** This was repository-only, read-only discovery. No test, endpoint, callback, container operation, video analysis, model download, or external request was run. No secret or environment value is included.

## 3. Current /analyze Response

**Verified.** AnalyzeRequest requires camelCase videoId, playerId, videoUrl, callbackUrl. Python names are video_id, player_id, video_url, callback_url; types are str, str, str, HttpUrl. All are required and extra="forbid" rejects unknown input. Evidence: src/schemas/analysis.py:10-18.

**Verified.** AnalyzeQueuedResponse has Python fields analysis_id, video_id, player_id, status. The first three serialize with camelCase aliases; status defaults to the non-null literal queued. Evidence: src/schemas/analysis.py:30-38. Test: tests/test_analyze.py:test_accepted_response_serializes_the_public_contract.

Exact success example (placeholder identifiers only):

~~~json
{"analysisId":"<generated-uuid>","videoId":"<request-videoId>","playerId":"<request-playerId>","status":"queued"}
~~~

## 4. Current Successful Callback Payload

**Verified.** _callback_payload maps a completed internal result through Public Rating V2, then forwards only summary, ratings, overall, and events plus IDs/status. Evidence: src/api/routes.py:157-173. CallbackService serializes model_dump(mode="json") with no aliases and no exclude_none, so callback names are snake_case. Evidence: src/services/callback_service.py:55-89.

Exact root payload shape (type placeholders, not invented scores):

~~~json
{
  "request_id":"<analysis_id>","video_id":"<videoId>","player_id":"<playerId>","status":"completed",
  "summary":{"possible_ball_interactions":0,"controlled_movements":0,"dribbles":0,"ball_losses":0,"passes":0,"shots":0,"ambiguous_events":0,"deduplicated_total_events":0},
  "ratings":{"technical_skill":"<PublicRatingValue>","physical_activity":"<PublicRatingValue>","ball_involvement":"<PublicRatingValue>","game_intelligence":"<PublicGameIntelligence>","soccer_intelligence":"<PublicRatingValue>","tactical_vision":"<PublicRatingValue>","mental_stability":"<PublicRatingValue>","professionalism":"<PublicRatingValue>","growth_potential":"<PublicRatingValue>","market_readiness":"<PublicRatingValue>","scalability":"<PublicRatingValue>"},
  "overall":"<PublicRatingValue>","events":{"timeline":[]},"error":null
}
~~~

**Verified contract.** request_id, video_id, player_id, status, summary, ratings, events are required/non-null. overall and error are nullable dictionaries defaulting to null. CallbackPayload has no aliases or explicit extra setting, so Pydantic default extra inputs are ignored. Evidence: src/services/callback_service.py:18-27. Public rating values require confidence/status/version and permit nullable [0,100] value; game intelligence adds required components/weights/limitations. Evidence: src/schemas/public_rating_v2.py:20-40.

## 5. Current Failure Callback Payload

**Verified.** Exceptions create the following payload:

~~~json
{"request_id":"<analysis_id>","video_id":"<video_id>","player_id":"<player_id>","status":"failed","summary":{},"ratings":{},"overall":null,"events":{},"error":{"code":"<exception-class-name>","message":"Analysis could not be completed."}}
~~~

Evidence: src/api/routes.py:236-258. overall:null is emitted because serialization does not exclude None; error is sanitized. Evidence: src/services/callback_service.py:64.

## 6. Current Field Contract

| Requested concept | Current callback location | Type / nullable | Conclusion |
| --- | --- | --- | --- |
| overall | root overall | PublicRatingValue; value nullable | Verified |
| technical | ratings.technical_skill | PublicRatingValue; value nullable | Verified: different name |
| physical | ratings.physical_activity | PublicRatingValue; value nullable | Verified: different name |
| ball_involvement | ratings.ball_involvement | PublicRatingValue; value nullable | Verified |
| game_intelligence | ratings.game_intelligence | PublicGameIntelligence; value nullable | Verified |

Moving scores into scores changes established locations, breaking producer contract and callback wiring assertions. Evidence: src/api/routes.py:157-173; tests/test_player_rating_wiring.py:167-204. The in-repo Apex handoff documents the same snake_case contract: docs/integration/backend_integration_handoff.md:64-90. **Unknown:** no Apex consumer schema, strict parser, or consumer test is present.

## 7. Five High-Level Score Calculations

| Score | Exact active formula/gate | Range and missing behavior | Serialization/tests |
| --- | --- | --- | --- |
| technical (technical_skill) | Requires controlled movement or dribble. value=100*clamp(mean available components - min(.25, sum loss confidence / positive components * .15)). Controlled weights confidence .40, displacement .25, direction .20, duration .15; dribble weights confidence .30, movement .25, proximity .20, straightness .15, turns .10. | [0,100]; no event evidence=null; no fixed rounding. | ratings.technical_skill; src/services/scoring/technical.py:30-101; tests/test_technical_scoring.py:30-47, tests/test_scoring_golden_regression.py:54-67 |
| physical (physical_activity) | Quality/visibility/duration/observation/accepted-ratio gates. Then 100*weighted normalized activity, active time, visibility, continuity, direction; configured weights must total 1. | [0,100]; failed gate=null; no fixed rounding; raw-image confidence cap. | ratings.physical_activity; src/services/scoring/physical_activity.py:22-149; tests/test_physical_scoring.py:21-37 |
| ball_involvement | Requires interaction coverage and count. 100*clamp((interaction time + controlled duration)/scale); confidence=coverage*interaction quality. | [0,100]; insufficient=null; no fixed rounding. | ratings.ball_involvement; src/services/player_rating/engine.py:108-146; tests/test_player_rating_engine.py |
| game_intelligence | Visible duration >=4 seconds and >=3 components. Weights ball .30, decision .20, spatial .20, efficiency .15, technical .15, renormalized over available components. | [0,100]; insufficient=null; confidence capped .65; no fixed rounding. | ratings.game_intelligence; src/services/player_rating/game_intelligence.py:91-137; src/config/scoring.py:5-15; tests/test_game_intelligence_engine.py |
| overall | Needs configured minimum supported categories, normalizes technical/physical/ball product weights across available values. Game intelligence is excluded. | [0,100]; insufficient=null; no fixed rounding. | root overall; src/services/player_rating/engine.py:148-205; tests/test_player_rating_engine.py and tests/test_player_rating_wiring.py |

**Verified.** Game intelligence is calculated during public projection, after the original summary, therefore it is not included in overall. Evidence: src/api/routes.py:928-933; src/api/public_rating_mapper.py:50-91.

## 8. Active Metric Inventory

**Verified active path:** route -> tracking/selection -> ball/movement/interaction/technical/pass/shot -> physical/technical -> rating summary -> Public Rating V2 -> callback. Evidence: src/api/routes.py:738-982,157-173.

| Metric/event | Detection and unit | Included in score | Limitation / test evidence |
| --- | --- | --- | --- |
| distance, average/max speed, average/max acceleration, stationary time, direction changes, intensity | bottom-center player boxes over time; pixels, pixels/sec, pixels/sec2 | physical and game movement proxies | camera/image-space; src/services/movement/analyzer.py:22-108,185-216; tests/test_movement.py |
| visibility, continuity, tracking confidence | selected player tracking | physical gates/confidence, game confidence | tracking dependent; src/services/scoring/physical_activity.py:51-111 |
| ball visible/proximity/quality | ball tracking and player-ball distance | ball involvement, game ball component | proximity is not possession; src/api/routes.py:691-737,866-870 |
| interaction count/duration/longest/confidence/coverage | aligned player-ball segments | ball involvement, game ball component | candidate only; src/api/routes.py:1334-1366; src/services/interactions/models.py |
| controlled movement/dribble/ball loss | technical-event analyzer | technical, ball duration, game proxies | candidate only; src/services/scoring/technical.py:30-97; tests/test_technical_events.py |
| pass candidate, receiver, distance, release/trajectory quality | possession -> release -> trajectory -> receiver | game decision count only | no completion/progression/key pass/assist; src/services/pass_detection.py:65-263; tests/integration/test_pass_detection.py |
| shot candidate, speeds, acceleration, preparation/release/follow-through | possession -> preparation -> release -> trajectory | game decision count only | no goal/target/on-target outcome; src/services/shot_detection.py:65-275; tests/integration/test_shot_detection.py |

**Verified absent active metrics.** No tackles, interceptions, recoveries, duels, pressing, assists, chances/key passes, goals, shots-on-target, calibrated speed/distance, team/opponent context, formation, pitch positioning, off-ball runs, space creation, or support movement. Public active event types are controlled_movement, dribble, ball_loss, pass, shot: src/schemas/public_rating_v2.py:14-17.

## 9. Seven-Axis Support Matrix

| Axis | Classification | Relevant metrics | Missing evidence and mapping conclusion | Confidence |
| --- | --- | --- | --- | --- |
| speed_and_fitness | PARTIALLY_SUPPORTED | pixel speed, acceleration, distance, activity | fitness/stamina/calibration absent; physical mapping invalid | High |
| ball_control_and_individual_skill | PARTIALLY_SUPPORTED | controlled, dribble, ball loss | technical is incomplete candidate proxy; mapping duplicates/relabels | High |
| passing_and_playmaking | PARTIALLY_SUPPORTED | pass candidates/receiver/trajectory | completion, progression, key pass, chance creation, assist absent | High |
| shooting_and_finishing | PARTIALLY_SUPPORTED | shot candidate/release/trajectory | goals/target/on-target/outcome absent | High |
| defending_and_duels | UNSUPPORTED | none | defensive/opponent evidence absent | High |
| tactical_intelligence_and_teamwork | PARTIALLY_SUPPORTED | game decision/spatial proxies, pass/shot counts | team/opponent/phase absent; game score says not tactical assessment | High |
| positioning_and_off_ball_movement | PARTIALLY_SUPPORTED | image-space movement/direction changes | role, pitch/team shape, off-ball evidence absent; proxy says not positioning | High |

No axis is SUPPORTED or DERIVABLE: a dedicated complete-axis score and adequate full evidence are not present.

## 10. Mapping Feasibility

| Target field | Current direct score | Relevant existing metrics | Support classification | Safe to populate now? | Recommended current value | Evidence |
| ------------ | -------------------- | ------------------------- | ---------------------- | --------------------- | ------------------------- | -------- |
| speed_and_fitness | None | image-space movement | PARTIALLY_SUPPORTED | No | null | src/services/movement/analyzer.py:22-108 |
| ball_control_and_individual_skill | None | controlled/dribble/loss | PARTIALLY_SUPPORTED | No | null | src/services/scoring/technical.py:30-101 |
| passing_and_playmaking | None | pass candidates | PARTIALLY_SUPPORTED | No | null | src/services/pass_detection.py:65-263 |
| shooting_and_finishing | None | shot candidates | PARTIALLY_SUPPORTED | No | null | src/services/shot_detection.py:65-275 |
| defending_and_duels | None | none | UNSUPPORTED | No | null | src/schemas/public_rating_v2.py:14-17 |
| tactical_intelligence_and_teamwork | None | game proxies | PARTIALLY_SUPPORTED | No | null | src/services/player_rating/game_intelligence.py:19-27 |
| positioning_and_off_ball_movement | None | movement proxy | PARTIALLY_SUPPORTED | No | null | src/services/player_rating/game_intelligence.py:256-300 |

## 11. Missing-Value Semantics

**Verified.** Public score values permit null and otherwise [0,100]. Engine states use null for insufficient_evidence/unsupported rather than zero. Evidence: src/schemas/public_rating_v2.py:20-40; src/services/player_rating/engine.py:207-235. Tests explicitly distinguish zero from missing evidence: tests/test_public_contract_stability.py:39-62.

**Recommendation (Inferred from verified contract).** number from 0 to 100 for a calculated supported score; null for unavailable new axes. Zero must not mean missing evidence.

## 12. Range, Normalization, and Rounding

**Verified.** External values are [0,100], confidence [0,1]; internals use [0,1] proxies. Clamping is in src/services/scoring/physical_activity.py:14-15,78-117; src/services/scoring/technical.py:43-57; src/services/player_rating/engine.py:183-239; src/services/player_rating/game_intelligence.py:380-388. Compact JSON has no fixed decimal precision or canonical ordering: src/services/callback_service.py:64. Two-decimal rounding would change existing behavior/golden output. Main score paths check finiteness; proof for every nested diagnostic is **Unknown**.

## 13. Backward-Compatibility Analysis

| Option | Response/callback effect | Apex/test risk | Result |
| --- | --- | --- | --- |
| A root detail fields | additive, current fields retained | Apex tolerance Unknown; root pollution | acceptable fallback |
| B preserve fields plus root detailed | additive and organized | Apex tolerance Unknown; callback/wiring tests need acceptance cases | recommended after confirmation |
| C move into scores | relocates root overall and ratings entries | breaks tests and likely Apex | reject |

Evidence: src/api/routes.py:157-173; tests/test_player_rating_wiring.py:167-204; docs/integration/backend_integration_handoff.md:64-90.

## 14. Schema-Version Recommendation

**Verified.** No callback schema_version, version header, or endpoint-version mechanism exists. Public V2 has internal version data, but callback forwards only selected maps. Evidence: src/api/public_rating_mapper.py:94-164; src/api/routes.py:157-173.

**Recommendation (Inferred).** Do not add schema_version "2.0" for a solely additive map; it is a separate unverified parser change.

## 15. Active and Dead-Code Findings

**Verified.** Pass and shot detectors are active and placed in internal completed results, but only their arbitrated counts/confidences feed game intelligence; there is no passing or shooting score. Evidence: src/api/routes.py:745-756,979-980; src/api/public_rating_mapper.py:185-237. No proposed active scoring module appears dead in route-to-callback trace. Domain sequence code is outside this trace (**Inferred**, static analysis).

## 16. Relevant Tests and Fixtures

| File | Current contract | Future impact |
| --- | --- | --- |
| tests/test_analyze.py | 202 queue response and lifecycle callbacks | add callback detailed case; /analyze unchanged |
| tests/test_callback_service.py | root JSON delivery | add nullable detailed serialization |
| tests/test_player_rating_wiring.py | mapper/callback values and overall | primary preservation + detail test |
| tests/test_public_contract_stability.py | zero/null and V2 serialization | extend only if detail shares schema |
| tests/test_player_rating_engine.py; physical/technical/game/golden tests | current formulas/gates | do not change for null-only contract |
| pass/shot/technical-event/movement tests | candidate production | retain; future score needs new sufficient/insufficient fixtures |

No callback snapshot/golden fixture, Apex consumer test, or detailed-score OpenAPI example was found in scope. Likely future docs: README.md and docs/integration/backend_integration_handoff.md.

## 17. Recommended Final Response Shape

**Inferred recommendation, conditional on Apex acceptance:**

~~~json
{
  "request_id":"example-analysis-id","video_id":"example-video-id","player_id":"example-player-id","status":"completed",
  "summary":{"...":"existing unchanged"},"ratings":{"...":"existing unchanged"},"overall":{"...":"existing unchanged"},"events":{"timeline":[]},"error":null,
  "detailed":{"speed_and_fitness":null,"ball_control_and_individual_skill":null,"passing_and_playmaking":null,"shooting_and_finishing":null,"defending_and_duels":null,"tactical_intelligence_and_teamwork":null,"positioning_and_off_ball_movement":null}
}
~~~

This is also the unsupported-fields example. Keep the exact failure payload in section 5 and omit detailed on failure. Do not include schema_version now.

## 18. Likely Implementation Files

**Inferred.** Later changes: src/services/callback_service.py, src/api/routes.py, a dedicated callback detail schema or src/schemas/public_rating_v2.py, tests/test_callback_service.py, tests/test_player_rating_wiring.py, tests/test_analyze.py, tests/test_public_contract_stability.py, README.md, and docs/integration/backend_integration_handoff.md.

Null-only contract work should not require detector, movement, physical/technical formula, Docker, configuration, deployment, or lock-file changes. Any numeric detailed axis requires a new scoring design and tests.

## 19. Risks and Unknowns

* **Unknown:** Apex parser acceptance of a new root key.
* **Verified:** pass/shot candidates lack playmaking/finishing outcome semantics.
* **Verified:** image-space movement cannot establish fitness or pitch positioning.
* **Verified:** game intelligence lists missing team/opponent/phase context: src/services/player_rating/game_intelligence.py:19-27.
* **Unknown:** product scoring definitions, validation datasets, evidence gates, and migration policy.

## 20. Questions Requiring Product or Apex Confirmation

1. Does Apex accept and ignore/persist an unknown nullable root detailed map?
2. Is callback root detailed the desired consumer location?
3. What validated definition/evidence gate applies to every new axis?
4. Is separate schema versioning required by Apex?

## 21. Exact Recommended Next Step

Obtain an Apex consumer contract test or written confirmation that an unknown nullable root detailed object is accepted while all existing callback fields remain unchanged; then approve scoring design for any non-null axis.

## 22. Evidence Index

Route/callback: src/api/routes.py:105-152,157-266,738-982. Callback DTO: src/services/callback_service.py:18-89. Schemas: src/schemas/analysis.py:10-38,320-358; src/schemas/public_rating_v2.py:20-82. Mapper: src/api/public_rating_mapper.py:32-164,185-237. Scores: src/services/player_rating/engine.py:35-239; src/services/scoring/physical_activity.py:22-149; src/services/scoring/technical.py:27-101; src/services/player_rating/game_intelligence.py:19-320. Detectors: src/services/movement/analyzer.py:22-216; src/services/pass_detection.py:65-263; src/services/shot_detection.py:65-275. Integration handoff: docs/integration/backend_integration_handoff.md:21-90.

### A. Final support table

| Target field | Populate now? | Source or value | Confidence | Reason |
| ------------ | ------------- | --------------- | ---------- | ------ |
| overall | Yes, existing only | root overall | High | Active weighted score |
| technical | Yes, existing only | ratings.technical_skill | High | Active candidate proxy |
| physical | Yes, existing only | ratings.physical_activity | High | Active visible movement proxy |
| ball_involvement | Yes, existing only | ratings.ball_involvement | High | Active interaction rating |
| game_intelligence | Yes, existing only | ratings.game_intelligence | High | Active provisional heuristic |
| speed_and_fitness | No | null | High | Fitness/calibration absent |
| ball_control_and_individual_skill | No | null | High | Technical proxy incomplete |
| passing_and_playmaking | No | null | High | Outcome/playmaking absent |
| shooting_and_finishing | No | null | High | Finishing outcome absent |
| defending_and_duels | No | null | High | No defensive pipeline |
| tactical_intelligence_and_teamwork | No | null | High | Team/tactical context absent |
| positioning_and_off_ball_movement | No | null | High | Position/off-ball evidence absent |

### B. Five most important findings

1. /analyze returns only queue acceptance; callback carries ratings.
2. Existing locations are root overall and ratings.*, not scores.
3. Moving fields into scores breaks established contract.
4. Existing physical/technical/game scores are not semantic equivalents of the detailed axes.
5. null, not zero, is the established missing-evidence meaning.

### C. Exact recommended next step

Obtain Apex confirmation, preferably backed by a consumer contract test, that it accepts an unknown nullable root detailed object without changes to current fields.

### D. Safety confirmation

* No application code was modified.
* No test was modified.
* No deployment/configuration file was modified.
* Only SUPER7_SCORING_RESPONSE_DISCOVERY.md was created or updated.
* No secrets were exposed.
* No production request or callback was sent.
* No scoring value was invented.

