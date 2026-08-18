# ADR-001: Evidence-Gated Detailed Player Ratings

## Status

Accepted

## Context

Super 7 exposes detailed player-rating fields including:

- `passing_and_playmaking`
- `shooting_and_finishing`
- `defending_and_duels`
- `tactical_intelligence_and_teamwork`
- `positioning_and_off_ball_movement`

Not all of these concepts can currently be measured defensibly from the available computer-vision evidence. The system must not generate a football rating when the underlying evidence does not support that interpretation.

## Decision

Detailed ratings are evidence-gated. A rating MUST remain `null` when Super 7 does not have sufficient direct evidence to support that metric.

### `passing_and_playmaking`

Currently derived only from qualifying, target-attributed, conflict-free pass-like events:

```
100 * mean(qualifying pass candidate confidence)
```

This represents the quality/confidence of observed pass-like evidence. It does not represent pass completion, passing accuracy, progressive passing, key passes, assists, chance creation, or full playmaking ability.

### `shooting_and_finishing`

Currently derived only from qualifying, target-attributed, conflict-free shot-like events:

```
100 * mean(qualifying shot candidate confidence)
```

A value such as `shooting_and_finishing = 81.5` represents the strength/quality of observed shot-like evidence detected by Super 7. It does **not** mean the player's real football finishing ability is 81.5/100.

The current system does not measure whether the shot was on target, whether it resulted in a goal, shot placement, goalkeeper interaction, shot difficulty, expected goals (xG), conversion rate, or finishing outcome quality. The public field name is therefore broader than the evidence currently supporting it; this limitation MUST be preserved until outcome-aware shooting evidence exists.

### Unsupported ratings

The following fields MUST remain `null` for now:

- `defending_and_duels`
- `tactical_intelligence_and_teamwork`
- `positioning_and_off_ball_movement`

They must not be populated using generic movement or unrelated proxy metrics.

## Rationale

Returning `null` is preferable to returning a precise-looking score that the current evidence cannot justify. Super 7 distinguishes what the system observed, what can reasonably be inferred from those observations, and what cannot yet be measured.

## Future evolution

`shooting_and_finishing` may become a true finishing-quality metric only when the pipeline supports goal detection, goal-mouth localization, on-target/off-target classification, shot outcome and placement, calibrated pitch position, and xG or shot difficulty. At that point this ADR must be reviewed and the scoring definition explicitly versioned. The existing field meaning must not silently change.

## Consequences

Positive consequences:

- avoids fabricated football intelligence;
- ratings remain explainable;
- `null` has an explicit semantic meaning; and
- future model improvements can be introduced deliberately.

Trade-offs:

- some public fields remain `null`;
- current field names may imply broader capability than the implementation provides; and
- richer ratings require additional CV evidence.

## Validation

A production shooting test produced `shooting_and_finishing = 81.50578877906025`, validating the technical path:

```
video -> player/ball evidence -> ShotDetector -> EventArbitrator
      -> target attribution -> DetailedRatingEngine -> callback -> Apex
```

This validates pipeline execution, not that 81.5 is a calibrated real-world finishing ability.
