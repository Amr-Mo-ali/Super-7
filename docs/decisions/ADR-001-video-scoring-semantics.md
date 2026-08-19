# ADR-001: Current video-scoring semantics

## Status

Accepted as a current-state record. This ADR documents code, not validation of football ability. Public Rating V2 exposes the top-level ratings; `detailed` is callback-only.

## Evidence rules

`null` is not zero and is not automatically a system failure. In the public rating contract it accompanies `insufficient_evidence` or `unsupported` and a reason; the callback detailed fields are nullable but do not carry their own status/reason. Event-detection confidence concerns candidate evidence, not player-skill quality. No repository evidence establishes scientific calibration or external validation for any field below.

| Field | Surface / implementation source | Current meaning and evidence | Status / confidence / limitations |
|---|---|---|---|
| `technical_skill` | Public V2; `services/scoring/technical.py`, `services/player_rating/engine.py` | 0–100 provisional aggregation of controlled-movement and dribble candidate components, reduced by ball-loss candidate confidence; pass/shot candidates do not enter it. | supported (as an implemented proxy); confidence is technical-event quality × mean positive-candidate confidence. Not a complete technical-skill assessment, confirmed actions, or calibrated ability; no score-level duration gate. |
| `physical_activity` | Public V2; `services/scoring/physical_activity.py`, `services/player_rating/engine.py` | 0–100 weighted visible image-space movement activity from intensity, active time, visibility, continuity, and direction. | supported (as visible activity); gated by movement quality, visibility, duration, observations, and accepted intervals. Confidence combines those evidence qualities and is capped at 0.75 for raw image space. It is not physiological fitness, speed, stamina, distance, or a complete fitness assessment. |
| `ball_involvement` | Public V2; `services/player_rating/engine.py` | `min(100, 100 × (possible interaction time + controlled-movement duration) / 5s)`. | provisional; needs interaction coverage at least the configured technical-event minimum and a nonzero possible-interaction count. Confidence is interaction coverage × interaction-analysis quality. Proximity does not prove possession; candidate events are not confirmed actions. |
| `game_intelligence` | Public V2; `services/player_rating/game_intelligence.py`, `api/public_rating_mapper.py` | Weighted, renormalized combination of ball involvement, decision-consistency, spatial-activity, movement-efficiency, and technical-involvement proxies. | provisional; needs ≥4 visible seconds and ≥3 available components. Confidence is weighted component confidence × duration × component coverage × missing-context factor, capped at 0.65. It is not a validated tactical-, cognitive-, or football-intelligence assessment; team, opponent, positional, and phase context are absent. |
| `overall` | Public V2; `services/player_rating/engine.py` | Weighted combination only of available `technical_skill`, `physical_activity`, and `ball_involvement`. | provisional; needs two categories. Confidence is the mean category confidence, reduced for duration and available-category coverage. It excludes `game_intelligence`; it is not overall football ability. See ADR-002. |
| `speed_and_fitness` | Callback `detailed`; `services/detailed_rating/engine.py` | Physical movement-intensity evidence × 100 when the physical response is provisional and finite. | provisional; no separate detailed confidence/status. Not speed, fitness, or physiological measurement. Null if the physical gate/status/evidence is unavailable. |
| `ball_control_and_individual_skill` | Callback `detailed`; `services/detailed_rating/engine.py` | Controlled-movement and dribble components averaged per event, with ball-loss penalty. | provisional; requires positive finite technical-event quality and a controlled/dribble candidate. No separate detailed confidence/status. Not validated broad individual skill or confirmed control. |
| `passing_and_playmaking` | Callback `detailed`; `services/detailed_rating/engine.py` | 100 × mean finite confidence of accepted, conflict-free, target-attributed pass candidates. | provisional; null without arbitration, target attribution, or qualifying candidates. Not proven pass completion, accuracy, assists, chance creation, or playmaking quality; candidate confidence is not player quality. |
| `shooting_and_finishing` | Callback `detailed`; `services/detailed_rating/engine.py` | 100 × mean finite confidence of accepted, conflict-free, target-attributed shot candidates. | provisional; null without arbitration, target attribution, or qualifying candidates. Not proven shot outcome, goals, on-target status, xG, placement, or finishing quality; candidate confidence is not player quality. |
| `defending_and_duels` | Callback `detailed`; `services/callback_service.py` | No calculation exists. | unsupported; always null. |
| `tactical_intelligence_and_teamwork` | Callback `detailed`; `services/callback_service.py` | No calculation exists. | unsupported; always null. |
| `positioning_and_off_ball_movement` | Callback `detailed`; `services/callback_service.py` | No calculation exists. | unsupported; always null. |

Unsupported detailed dimensions must remain null until defensible evidence primitives, calculations, gates, confidence semantics, tests, and a validation plan exist.

## Validation and limitations

Tests establish deterministic formulas, gating, bounds, target attribution, and null behavior (`tests/test_player_rating_engine.py`, `tests/test_detailed_rating.py`, and `tests/test_game_intelligence_engine.py`). They do not validate any score against labelled human assessments, physiological measurements, pass outcomes, shot outcomes, or football performance. The term “supported” above therefore means implemented with direct code evidence, not scientifically calibrated.

## Mismatch to preserve

`PlayerRatingEngine.summarize()` exposes an optional game-intelligence parameter, but this is not safe Overall support. Production route wiring does not pass it and Public V2 calculates game intelligence separately. If an available game-intelligence result is passed, it enters the available category set while `_overall()` has no corresponding weight, causing `KeyError`. Consequently, game intelligence is public but excluded from the current production Overall.
