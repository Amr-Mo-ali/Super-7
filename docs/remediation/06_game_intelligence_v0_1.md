# Game Intelligence V0.1

This phase adds a deterministic, video-based heuristic indicator for an early dashboard. It is not a validated football-intelligence, cognitive, scouting, or tactical assessment. Team, opponent, positional, and phase-of-play context are absent; later calibration against labelled human evaluation data is required.

## Inputs and components

Only completed pipeline summaries are used: player visibility/continuity, normalized movement evidence, ball/interaction quality, technical-event candidates, and existing technical score. No frames, trajectories, detector, tracker, video, GPU, or network work is invoked.

- Ball involvement: weighted proximity (35%), interaction duration (25%), frequency (20%), bounded longest segment (10%), and interaction confidence (10%). Proximity is not possession.
- Decision consistency: existing technical score when available, otherwise accepted positive candidate evidence, reduced by confident ball-loss evidence. It is only an observable event-consistency proxy.
- Spatial activity proxy: movement intensity (35%), active ratio (25%), direction component (20%), and normalized direction-change rate (20%). It is not spatial awareness or tactical positioning.
- Movement efficiency proxy: intensity (35%), direction (25%), active ratio (20%), and continuity (20%). It does not measure speed, fitness, stamina, or real distance.
- Technical involvement: adapts the existing technical score; it does not replace technical scoring.

Product weights are ball involvement .30, decision consistency .20, spatial activity proxy .20, movement efficiency proxy .15, and technical involvement .15. Available weights are renormalized; unavailable evidence never becomes zero.

## Gates and confidence

Any result needs at least 4 visible seconds. A component needs its relevant quality/evidence gates; at least three available components are required. Ball involvement requires ball and interaction quality at least .45; movement proxies require movement quality at least .55. Decision consistency needs accepted technical evidence or technical score.

Confidence is the weighted component-confidence mean × min(visible duration / 20 s, 1) × available-component coverage × .75 missing-context factor, capped at .65. Video quality reduces confidence and gates availability rather than directly reducing the score.

Pass/shot time overlap is not arbitrated or double-counted in the decision proxy, and adds an explicit limitation. All results include `heuristic_estimation`, camera/view/context limitations, and `candidate_events_are_not_confirmed_actions`; shorter than 20 seconds adds `short_video`.

## Public output and risks

Public Rating V2 exposes `ratings.game_intelligence` with value, confidence, provisional status, version, compact component values/statuses, effective weights, and limitations. It omits diagnostic evidence and local paths. Level labels are the existing neutral `very_low` through `excellent` boundaries.

Tests cover gates, exact three-component normalization, no zero filling, clamping/non-finite safety, score/confidence separation, cap, overlap limitation, deterministic output, stable version/levels, and API serialization through the existing V2 suite. The next phase is event arbitration and calibration against labelled, multi-context human evaluation data—not stronger claims from this heuristic.
