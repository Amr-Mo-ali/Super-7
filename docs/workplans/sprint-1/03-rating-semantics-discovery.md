> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Rating semantics discovery

| Field | Source / formula / gate | Defensible meaning | Overall? | Risk | Classification |
|---|---|---|---:|---|---|
| `overall`, `overall_confidence` | `PlayerRatingEngine`: technical .45, physical .30, ball .25; renormalize available weights; at least two; confidence = mean category confidence × duration/5s cap × available/3. | Provisional aggregation, not total football ability/calibration. | — | Numeric with limited categories and an unverified target. | Implemented and production-wired |
| `technical_skill` | Technical controlled/dribble candidate components and ball-loss penalty, gated finite/evidence conditions. | Candidate-event proxy, not confirmed technical skill. | yes | Target can be wrong; no skill validation. | Implemented and production-wired |
| `physical_activity` | Rule-based image-space movement intensity, activity, visibility, continuity and direction; quality/observation gates. | Visible movement activity, not metres, speed, fitness, stamina or physiology. | yes | Camera/image geometry. | Implemented and production-wired |
| `ball_involvement` | Interaction coverage and nonzero-count gate; interaction + controlled movement duration saturates at 5s. | Proximity/interaction observation, not possession. | yes | Candidate evidence and target error. | Implemented and production-wired |
| `game_intelligence` | V0.1 weighted/renormalized proxies; ≥4 visible seconds and ≥3 components; confidence cap .65. | Provisional observational heuristic, not intelligence/tactics/cognition. | no | Team/opponent/pitch/phase context absent. | Implemented and production-wired |
| `speed_and_fitness` | Detailed engine maps available physical movement intensity ×100. | Visible movement activity, not speed/fitness. | no | Misleading label. | Implemented and production-wired |
| `ball_control_and_individual_skill` | Controlled/dribble component and ball-loss penalty; qualifying positive evidence. | Candidate proxy, not validated broad skill. | no | Candidate quality is limited. | Implemented and production-wired |
| `passing_and_playmaking` | Mean confidence of accepted conflict-free target-attributed pass candidates ×100. | Candidate-event detection confidence, not completion/value/playmaking. | no | Semantic mixing in label. | Implemented and production-wired |
| `shooting_and_finishing` | Mean confidence of accepted conflict-free target-attributed shot candidates ×100. | Candidate-event detection confidence, not outcome/difficulty/finishing. | no | Semantic mixing in label. | Implemented and production-wired |
| `defending_and_duels`; `tactical_intelligence_and_teamwork`; `positioning_and_off_ball_movement` | Callback detailed fields have no calculation. | Unsupported, null. | no | Must remain null. | Implemented and production-wired |

Overall deliberately excludes game intelligence in production wiring. Technical is not mandatory:
physical plus ball involvement use renormalized .30/.25 weights. Thus a high number with only two
categories is arithmetically expected; missing values are not zero-filled. Passing/shot data flows
candidate → arbitration acceptance/conflict/target attribution → event confidence → detailed
numeric field. That confidence is detection/evidence confidence, not execution quality; completion,
outcome and difficulty are unknown. This is the clearest event-versus-skill semantic mix.

Movement uses tracked bounding-box bottom-centre positions in image coordinates and derivatives in
pixels; optional trajectory/camera components do not establish pitch/world coordinates or metres.
`null` is distinct from zero and failed analysis. Public V2 rating states/reasons provide richer
availability than `value is not None`; callback detailed fields are nullable without per-field
reason/status. Existing handoff records one 14.17-second drill observation as empirical only, not
validation; this audit adds no new observation.
