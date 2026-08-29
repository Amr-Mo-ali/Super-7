> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Rating semantics discovery

The table audits runtime baseline `7920375b915e852486643df8eb5bf27bf8fb09ae`. Paths/symbols and
tests are source evidence; no listed test is claimed to have passed in this discovery session.
Top-level values are produced by `src/api/public_rating_mapper.py:public_rating_v2`; callback
detailed values are constructed by `src/api/routes.py:_callback_payload` through
`src/services/detailed_rating/engine.py:DetailedRatingEngine.evaluate`.

| Field | Exact source path and symbol | Source evidence / current formula or aggregation | Current evidence gate | Current public meaning | Actual defensible meaning | Included in Overall | Known risk | Evidence classification | Relevant tests |
|---|---|---|---|---|---|---:|---|---|---|
| `overall` | `src/services/player_rating/engine.py:PlayerRatingEngine._overall`; mapped by `public_rating_v2` | Available technical .45, physical .30, ball .25 weights renormalized; weighted average. | At least two available categories. | Overall rating. | Provisional aggregate of video proxies, not total ability/calibration. | — | Numeric result with only two categories and no target-eligibility gate. | Implemented and production-wired | `tests/test_player_rating_engine.py`; `tests/test_public_contract_stability.py` |
| `overall_confidence` | `src/services/player_rating/engine.py:_overall` | Mean category confidence × `min(1, duration/5)` × available/3. | Same availability gate as Overall; finite inputs. | Overall confidence. | Evidence/calculation confidence, not calibrated certainty or skill quality. | — | Can be low while Overall is high. | Implemented and production-wired | `tests/test_player_rating_engine.py` |
| `technical_skill` | `src/services/scoring/technical.py:TechnicalScorer`; `src/services/player_rating/engine.py:summarize` | Controlled/dribble candidate components less ball-loss penalty. | Finite technical score/confidence and technical evidence gate. | Technical skill rating. | Provisional candidate-event proxy, not confirmed technical skill. | yes | Wrong selected person; no ability validation. | Implemented and production-wired | `tests/test_technical_scoring.py`; `tests/test_player_rating_engine.py` |
| `physical_activity` | `src/services/scoring/physical_activity.py:RuleBasedPhysicalActivityScorer`; `PlayerRatingEngine.summarize` | Weighted movement intensity, active time, visibility, continuity and direction. | Movement quality, visibility/duration/observations and accepted interval gates. | Physical activity rating. | Visible image-space movement, not metres, real speed, fitness, stamina or physiology. | yes | Camera/image geometry and target attribution. | Implemented and production-wired | `tests/test_physical_scoring.py`; `tests/test_player_rating_engine.py` |
| `ball_involvement` | `src/services/player_rating/engine.py:PlayerRatingEngine._ball_involvement` | `min(100,100*(possible interaction + controlled movement duration)/5)`. | Configured interaction coverage minimum and nonzero possible-interaction count. | Ball involvement rating. | Ball proximity/interaction observation, not possession. | yes | Candidate evidence and target error. | Implemented and production-wired | `tests/test_player_rating_engine.py`; `tests/test_interactions.py` |
| `game_intelligence` | `src/services/player_rating/game_intelligence.py:GameIntelligenceEngine.evaluate`; `src/api/public_rating_mapper.py:game_intelligence_result` | Available ball, decision, spatial, movement-efficiency and technical proxies, weighted/renormalized. | ≥4 visible seconds, ≥3 components, finite values; confidence cap .65. | Game intelligence rating. | Provisional observational heuristic, not tactical intelligence/cognition. | no | Team/opponent/pitch/phase context absent; documented missing-weight `KeyError` if passed to `summarize`. | Implemented and production-wired | `tests/test_game_intelligence_engine.py`; `tests/test_public_contract_stability.py` |
| `speed_and_fitness` | `src/services/detailed_rating/engine.py:DetailedRatingEngine.evaluate` | Available physical movement intensity ×100. | Physical result must be provisional/finite. | Detailed speed and fitness. | Visible movement activity only, not speed or fitness. | no | Label overclaims available evidence. | Implemented and production-wired | `tests/test_detailed_rating.py` |
| `ball_control_and_individual_skill` | `src/services/detailed_rating/engine.py:_ball_control` | Controlled/dribble components less ball-loss penalty. | Positive finite technical-event quality and controlled/dribble candidate. | Detailed ball control/individual skill. | Candidate proxy, not validated broad skill. | no | Event candidate and target limitations. | Implemented and production-wired | `tests/test_detailed_rating.py` |
| `passing_and_playmaking` | `src/services/detailed_rating/engine.py:_event_score`; `src/api/public_rating_mapper.py:event_arbitration` | Mean confidence of accepted, conflict-free target-attributed pass candidates ×100. | Arbitration, target attribution and qualifying finite candidate. | Detailed passing/playmaking. | Candidate-event detection confidence, not completion, value or playmaking quality. | no | Event confidence sits beneath a skill label. | Implemented and production-wired | `tests/test_detailed_rating.py`; `tests/test_event_arbitration.py` |
| `shooting_and_finishing` | `src/services/detailed_rating/engine.py:_event_score`; `public_rating_mapper.py:event_arbitration` | Mean confidence of accepted, conflict-free target-attributed shot candidates ×100. | Arbitration, target attribution and qualifying finite candidate. | Detailed shooting/finishing. | Candidate-event detection confidence, not outcome, difficulty or finishing quality. | no | Event confidence sits beneath a skill label. | Implemented and production-wired | `tests/test_detailed_rating.py`; `tests/test_event_arbitration.py` |
| `defending_and_duels` | `src/services/callback_service.py:DetailedRatings` | No calculation supplied to detailed mapper. | Unsupported. | Detailed field. | Unsupported; null. | no | Must not be zero-filled. | Implemented and production-wired | `tests/test_callback_service.py`; `tests/test_detailed_rating.py` |
| `tactical_intelligence_and_teamwork` | `src/services/callback_service.py:DetailedRatings` | No calculation supplied to detailed mapper. | Unsupported. | Detailed field. | Unsupported; null. | no | Must not be zero-filled. | Implemented and production-wired | `tests/test_callback_service.py`; `tests/test_detailed_rating.py` |
| `positioning_and_off_ball_movement` | `src/services/callback_service.py:DetailedRatings` | No calculation supplied to detailed mapper. | Unsupported. | Detailed field. | Unsupported; null. | no | Must not be zero-filled. | Implemented and production-wired | `tests/test_callback_service.py`; `tests/test_detailed_rating.py` |

Overall deliberately excludes Game Intelligence in production wiring. Technical is not mandatory:
physical plus ball involvement use renormalized .30/.25 weights. Passing/shot evidence flows
candidate → arbitration acceptance/conflict/target attribution → event confidence → detailed
numeric field. That confidence is detection/evidence confidence, not execution quality; completion,
outcome and difficulty are unknown.

The proposed Sprint 1 hierarchy requires Overall to consume explicit per-rating availability
decisions, including initial target eligibility, rather than only testing whether a Python value is
non-null. This is **Proposed** and does not change the inspected formula.

## Proposed / approved semantic decision

After implementation approval, target establishment gates every player-attributed rating. Proposed
Overall requires an established target, available Technical, and at least one additional available
core category (Physical Activity or Ball Involvement). This is not implemented. Public Game
Intelligence is proposed unavailable/null for MVP and remains excluded from Overall. Physical
Activity remains available only for an established target when its existing gate passes, with the
defensible meaning image-space visual movement/activity indicator. Event confidence remains separate
from skill; it cannot make a skill rating eligible by itself.

Movement uses `src/services/movement/analyzer.py:BottomCenterMovementAnalyzer` tracked bounding-box
bottom-centre image coordinates and pixel derivatives. Optional trajectory/camera components do not
establish pitch/world coordinates or metres. `null` is distinct from zero and failed analysis;
Public V2 status/reason is richer than `value is not None`, whereas callback detailed fields lack
per-field availability reason/status. Existing handoff's 14.17-second drill is an **Empirically
observed** single observation, not validation; no new observation was made here.
