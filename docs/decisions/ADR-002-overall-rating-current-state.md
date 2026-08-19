# ADR-002: Overall rating current state

## Status

Accepted as an audit of the active implementation in `services/player_rating/engine.py`; it changes no formula.

## Current calculation

`overall` is numeric only when at least two `available` categories exist. The only configured weights are `technical_skill` (0.45), `physical_activity` (0.30), and `ball_involvement` (0.25). Available weights are renormalized to sum to 1; missing categories are not zero-filled.

Production route wiring calls `PlayerRatingEngine.summarize()` without `game_intelligence`; Public V2 calculates game intelligence separately, so it does not enter the current production overall. This must not be read as safe support for game intelligence in overall: if an available game-intelligence result is passed to `summarize()`, it is included in `available`, but `_overall()` indexes a weights map with no `game_intelligence` entry. That path raises `KeyError` before an overall result is returned. An insufficient game-intelligence result is not included in `available` and does not trigger this path.

The value is the weighted average of available category values. Confidence is:

```text
mean(available category confidence)
× min(1, evidence_duration_seconds / 5)
× available_category_count / 3
```

`evidence_duration_seconds` is the maximum of physical movement duration, possible interaction duration, and any controlled-movement duration. Overall uses the neutral bands `very_low` (<20), `low` (<35), `developing` (<50), `moderate` (<65), `good` (<80), `very_good` (<90), and `excellent` (≥90).

| Edge case | Current result |
|---|---|
| Only one category available | `overall = null`, `insufficient_evidence`, reason `insufficient_supported_categories`. |
| Two categories available | Numeric weighted average using their renormalized weights; lower coverage factor reduces confidence. |
| Technical missing | If physical and ball involvement are available, numeric overall with weights 0.30/0.25 renormalized. |
| Physical missing | If technical and ball involvement are available, numeric overall with weights 0.45/0.25 renormalized. |
| Ball involvement missing | If technical and physical are available, numeric overall with weights 0.45/0.30 renormalized. |
| Game intelligence available | In current production it is calculated outside `PlayerRatingEngine` and excluded from overall. If supplied as an available `summarize()` argument, current `_overall()` raises `KeyError` because no weight is configured. |
| High value with low confidence | Possible: category values and confidence are separate; short evidence and fewer categories reduce confidence without changing value. |
| Short video | Possible numeric overall if category gates pass; duration reduces only overall confidence, while technical has no score-level duration gate. |
| Unsupported detailed dimensions | Callback detailed nulls neither enter top-level categories nor overall. |

## Interpretation limits

This can appear high when a small set of available category proxies are high, including with incomplete evidence or low confidence. Weight renormalization intentionally avoids treating absence as poor performance, but also means values with different available inputs are not directly equivalent. It must not be marketed as overall football ability without a product definition, broader evidence, labelled-data validation, calibration, and a versioned decision.

## Unresolved product decisions

- Whether game intelligence should ever enter overall, and under what validated meaning and weight.
- If game intelligence is ever passed into `PlayerRatingEngine`, how to prevent the current missing-weight failure while preserving explicit product semantics.
- Whether technical scoring needs duration, coverage, or event-count gates.
- Whether a numeric overall should require all three current categories or a confidence threshold.
- How scores should be calibrated and compared across camera views, positions, match contexts, and video durations.
