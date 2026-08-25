# Scoring and product semantics

All public rating values are callback projections from [`public_rating_mapper.py`](../../src/api/public_rating_mapper.py) and [`DetailedRatingEngine`](../../src/services/detailed_rating/engine.py). `null` is not zero and does not mean failed analysis: it means unsupported or insufficient evidence. Event detection confidence is not player skill, pass completion/playmaking quality, or finishing/shot outcome.

| Public field | Current evidence/formula and gate | Overall? | Status and meaning; must not mean |
|---|---|---:|---|
| `overall` / `overall_confidence` | [`PlayerRatingEngine`](../../src/services/player_rating/engine.py): available technical, physical and ball-involvement categories; weights 0.45/0.30/0.25 renormalized to available categories; at least 2 categories; confidence = mean category confidence × duration factor (5 s scale) × available/3 | n/a | Provisional aggregation; not total football ability or calibrated ability |
| `technical_skill` | Candidate controlled/dribble/ball-loss evidence from technical scorer; missing value/confidence is gated null | Yes | Provisional candidate-event estimate; not confirmed technical skill |
| `physical_activity` | Visible image-space movement score with movement evidence/quality gates | Yes | Observed activity in this video; not speed, fitness, physiology or comparable distance |
| `ball_involvement` | Interaction coverage must meet configured minimum and count > 0; duration includes interaction plus controlled movement, saturating at 5 s | Yes | Ball proximity/interaction observation; not possession |
| `game_intelligence` | V0.1 heuristic combines available ball, decision, spatial, movement-efficiency and technical components; ≥4 s visible, ≥3 components; weighted/renormalized; confidence capped 0.65 | **No** | Public provisional observational heuristic; not intelligence, tactics, cognition or validated assessment |
| `speed_and_fitness` | Physical evidence's movement intensity only, after physical gate | No | Provisional visible movement activity; not fitness |
| `ball_control_and_individual_skill` | Existing controlled/dribble components minus ball-loss penalty; technical quality and positive candidate evidence required | No | Provisional candidate evidence; not validated ball control |
| `passing_and_playmaking` | Mean confidence of accepted, target-track-attributed pass candidates after arbitration | No | Candidate-event confidence; not pass completion, value or playmaking quality |
| `shooting_and_finishing` | Mean confidence of accepted, target-track-attributed shot candidates after arbitration | No | Candidate-event confidence; not finishing, shot outcome or difficulty |
| `defending_and_duels` | No implementation found | No | Unsupported/null |
| `tactical_intelligence_and_teamwork` | No implementation found | No | Unsupported/null |
| `positioning_and_off_ball_movement` | No implementation found | No | Unsupported/null |

The Overall calculation intentionally excludes game intelligence: `summarize()` passes only technical/physical/ball involvement into `_overall`. Technical is **not mandatory**. Thus, when technical is null, physical + ball involvement may be renormalized to 0.5455/0.4545 and still yield a high result. This is arithmetic correctness, not product validity. Overall confidence falls with missing categories and short evidence, but it is not a calibration guarantee.

Empirical example (one supplied production observation, not a benchmark): a 14.17-second single-player shooting drill without goalkeeper, defender, teammates, pressure or tactical alternatives returned technical `null`, physical activity about 86.35, ball involvement 100, game intelligence about 79.00, shooting/finishing about 81.51, overall about 92.56, and overall confidence about 0.412. Renormalization can explain the arithmetic; presenting it as broad football ability would be misleading.

Target-player correctness is P0: `playerId` is Apex request identity, not visual proof. Automatic selection uses visual track/segment evidence; it neither verifies it belongs to the requested person nor preserves re-identification history across track IDs. Scoring can therefore be internally correct for the wrong person. Do not describe target selection as solved.

Known limitations apply throughout: single camera/image space, incomplete field/team/opponent context, candidate events, unvalidated thresholds, no labelled validation dataset, no precision/recall figures and no score calibration. Source and tests are authoritative; see [ADR-001](../decisions/ADR-001-video-scoring-semantics.md), [ADR-002](../decisions/ADR-002-overall-rating-current-state.md), and [ADR-003](../decisions/ADR-003-null-and-evidence-policy.md).
