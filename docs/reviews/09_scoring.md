# Phase 9 — Scoring Review

**Scope:** technical score, physical score, score confidence and levels, evidence
sufficiency, and response explanation.

**Review constraint:** no production scoring formula, threshold, or scoring outcome
was changed. No production video inference was run.

## Files reviewed

- `src/services/scoring/technical.py`
- `src/services/scoring/physical_activity.py`
- `src/services/scoring/level_mapper.py`
- `src/services/scoring/models.py`
- `src/services/feature_extractor.py`
- `src/schemas/analysis.py`
- `src/api/routes.py`
- `src/core/config.py`
- `tests/test_technical_scoring.py`
- `tests/test_physical_scoring.py`

## Formula and dependency inventory

### Technical score — `technical_scoring_v0.1`

**Inputs**

- accepted `ControlledMovementCandidate` records;
- accepted `DribbleCandidate` records;
- accepted `BallLossCandidate` records;
- `TechnicalEventDiagnostics.technical_event_analysis_quality`.

No pass or shot candidate is currently an input to technical scoring.

**Sufficiency gate**

The result is unavailable when technical-event analysis is missing or when both the
controlled-movement and dribble candidate collections are empty. Ball-loss evidence
alone does not produce a score.

**Per-controlled-movement value**

```text
0.40 × confidence
+ 0.25 × min(1, normalized_player_displacement)
+ 0.20 × max(0, direction_similarity or 0)
+ 0.15 × min(1, duration_seconds / 2)
```

The controlled component is the arithmetic mean of accepted controlled-movement
values.

**Per-dribble value**

```text
0.30 × confidence
+ 0.25 × movement_evidence_component
+ 0.20 × proximity_persistence
+ 0.15 × path_straightness
+ 0.10 × min(1, direction_changes / 3)
```

The dribble component is the arithmetic mean of accepted dribble values.

**Final technical score**

```text
positive_base = mean(non-empty controlled component, non-empty dribble component)
ball_loss_penalty = min(0.25, sum(ball-loss confidence) / positive_component_count × 0.15)
score = clamp_0_100((positive_base - ball_loss_penalty) × 100)
confidence = clamp_0_1(
    technical_event_analysis_quality
    × mean(confidence of accepted controlled and dribble candidates)
)
```

The returned `evidence` map contains counts, not score components or a measure of
source coverage. `quality` is the upstream technical-event analysis quality and is
not the final score confidence.

### Physical score — `physical_activity_video_v0.1`

**Inputs**

- `MovementResult` and its ordered trajectory and metrics;
- target-track visibility ratio, visible-frame count, longest continuous segment,
  and mean track confidence;
- movement quality;
- movement source (`raw_image_space` determines the confidence cap).

**Sufficiency gate**

The score is `insufficient_evidence` if movement or movement quality is absent, or
if any of the following fail:

```text
movement_quality >= 0.55
visibility_ratio >= 0.20
trajectory duration >= 3.0 seconds
trajectory observations >= 30
observations / (observations + rejected_position_jumps) >= 0.60
```

The scorer also validates that the five configured score weights are non-negative
and sum to one.

**Derived evidence**

```text
duration = last trajectory timestamp - first trajectory timestamp
accepted_interval_ratio = observations / max(observations + rejected_position_jumps, 1)
active_time_ratio = clamp_0_1(1 - stationary_time_seconds / duration)
continuity_ratio = clamp_0_1(longest_segment / visible_frames)  [0 when visible_frames is 0]
direction_rate = direction_changes / duration
direction_component = direction_rate / (direction_rate + 0.5)  [0 when rate is 0]
```

`movement_intensity`, visibility, movement quality, and direction are clamped to
`[0, 1]` when captured as evidence.

**Final physical score**

```text
raw = 0.35 × movement_intensity
    + 0.25 × active_time_ratio
    + 0.15 × visibility_ratio
    + 0.15 × continuity_ratio
    + 0.10 × direction_component
score = clamp_0_1(raw) × 100
```

**Physical confidence**

```text
base_confidence = mean(
    movement_analysis_quality,
    clamp_0_1(track_confidence),
    visibility_ratio,
    accepted_interval_ratio,
    clamp_0_1(duration / 3.0)
)
confidence = min(base_confidence, 0.75) when source is raw_image_space
             else base_confidence
```

The final confidence is clamped to `[0,1]`. The raw-image cap is explicitly exposed
as `confidence_capped` in diagnostics, but only indirectly in the public response
through the resulting confidence and limitations.

**Score levels**

Physical scores map to the nearest fixed midpoint, with a lower numerical level on
ties:

```text
1 beginner     25.0       2 acceptable  55.0
3 average      65.0       4 good        75.0
5 very_good    85.0       6 excellent   92.0
7 exceptional  97.5
```

## Findings

### Critical

None found within this scope.

### High

1. **New pass and shot evidence does not affect technical scoring.** The current
   technical score deliberately consumes only controlled movement, dribble, and ball
   loss candidates. Adding accepted pass and shot detection therefore changes the
   response without changing technical-score evidence or explanation. This is an
   unsupported completeness implication if clients interpret the technical score as
   covering all detected technical events. Adding events to the formula is outside
   this review and must be a versioned scoring decision.

2. **The successful technical-score response does not explain the result.**
   `TechnicalScoreResponse` exposes value, confidence, status, version, and count
   evidence, but no reason, limitations, score components, or statement that the
   result excludes pass and shot evidence. The physical response has an explanation
   and limitations; this asymmetry makes technical certainty easier to overread.

### Medium

1. **Technical confidence is not an evidence-sufficiency confidence.** It combines
   upstream analysis quality with the mean confidence of accepted positive events.
   It does not include event count, duration, coverage, or rejection rate. A single
   accepted candidate can yield a confident provisional result. The sufficiency gate
   only checks presence of a controlled-movement or dribble candidate.

2. **Short-video instability remains possible in technical scoring.** The physical
   score requires at least three seconds and 30 observations. Technical scoring has
   no equivalent scoring-level duration/coverage gate beyond the upstream candidate
   rules. Two clips with materially different observation coverage can obtain the
   same aggregation pattern.

3. **Per-family averaging gives each event family equal weight regardless of event
   count.** One controlled event and many dribbles are averaged as two components,
   not as all events. This is a valid formula but should be documented in response
   explanation because users may assume every candidate has equal influence.

4. **Physical score labels imply discrete levels from a nearest-midpoint mapping.**
   The labels are not threshold bands. A score near a midpoint maps to that level,
   and ties map downward. `level_midpoint` is exposed, but the response explanation
   does not describe this interpretation.

5. **Physical movement units are safely qualified but not fully self-describing in
   score evidence.** The response limitations state image-space measurements and
   lack of camera-motion compensation, but numeric physical evidence fields do not
   name units. `movement_duration_seconds` is clear; intensity and ratios are unitless.
   The score must not be interpreted as a physical fitness measurement.

### Low

1. **Clamping is distributed across score construction.** The physical scorer clamps
   each derived evidence value and final score/confidence; the technical scorer clamps
   final score/confidence while clamping selected inputs inside components. This is
   behaviorally safe but makes it harder to distinguish source clipping from final
   output clipping in diagnostics.

2. **Formula and response formatting are largely separate, with one presentation
   coupling.** Scoring formulas live in the scoring services and Pydantic conversion
   lives in `FeatureExtractor`. However, the technical version constant is imported
   by the extractor rather than carried by `TechnicalScoreResult`, unlike physical
   scoring. This can lead to a response version that is detached from a future result
   object implementation.

3. **`technical_evidence_score` is misleadingly named.** Route diagnostics calculate
   it as the sum of event-count values in the technical evidence map. It is a count
   total, not a normalized score, confidence, or quality measure.

## Response explanation assessment

Physical scoring has a conservative explanation and explicit limitations, including
image-space and non-fitness-assessment language. The physical status also clearly
separates `provisional_video_based` from `insufficient_evidence`.

Technical scoring is labelled `provisional_event_based`, but a successful response
has no comparable limitations or explanation. Unavailable technical scoring does
provide a reason through the unsupported-metric branch. The public response therefore
communicates failure more clearly than the scope of a successful score.

## Golden regression tests

Added `tests/test_scoring_golden_regression.py` with fixed, inference-free baselines
for the current technical and physical formulas. The technical golden case covers
controlled movement, dribble, ball-loss penalty, confidence, and count evidence. The
physical golden case covers weighted evidence, nearest-midpoint level mapping, and
the raw-image confidence cap.

## Changes made

- Added `tests/test_scoring_golden_regression.py`.
- Added this report.

No scoring formulas, thresholds, response contracts, or production behavior changed.

## Remaining risks

Score confidence can be understood as certainty about player skill rather than the
limited quality of the available event/movement evidence. Pass and shot candidates
remain excluded from technical scoring until a separately versioned scoring decision
defines how they should contribute. Camera movement and short observations can still
limit physical-score meaning despite the current provisional labels and confidence cap.
