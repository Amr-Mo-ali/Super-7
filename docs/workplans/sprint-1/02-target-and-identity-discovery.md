> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Target and identity discovery

## Three separate identities

| Concept | Current evidence | Meaning | Classification |
|---|---|---|---|
| Business identity | `AnalyzeRequest.player_id`, queue job, acceptance response and callback payload | Apex-supplied request/player association. | Implemented and production-wired |
| Visual target | `Selection` / selected segment passed to analysis | The visually easiest qualifying temporary track/segment to analyze. | Implemented and production-wired |
| Tracking identity | ByteTrack `Track.track_id`, dictionaries keyed by track ID within `TrackingRun` | Temporary association created by a tracker during one analysis run. | Implemented and production-wired |

The relationship requiring review is: `playerId → associated analysis request → selected visual
target → internally represented by temporary track_id`. There is no implemented evidence for
`playerId == track_id`; that claim is explicitly unsupported.

`playerId` enters `src/schemas/analysis.py`, is carried by `AnalysisJob`/child request, and is
echoed in acceptance and callback data. Source inspection finds no use of it by ByteTrack, the
selector, candidate ranking, or a visual verification function. It is therefore not validated
against visual evidence.

`ByteTrackTracker` instantiates state per `DetectionOnlyPlayerTracker.analyze()` invocation. IDs
are assigned from detector associations, have no cross-analysis persistence, and can fragment or
change under occlusion/association failure. `tracklet_stitching.py` is a no-op boundary, not
implemented Re-ID. Tracking diagnostics expose counts, not a persistent visual identity.

## Current selector

In default `segment` mode, `build_segments()` breaks a track on excessive gaps or normalized
centre jumps. A segment is rejected for insufficient visible frames/duration, low mean detection
confidence or low quality. Quality weights are duration 0.25, continuity 0.25, detection
confidence 0.20, bounding-box height 0.15, stability 0.10 and ball proximity 0.05.
`rank_segments()` sorts qualifying segments descending by quality; `select_segment()` returns the
first. There is no second-candidate dominance/margin threshold and ties preserve input/order
effects of Python stable sorting. No qualifying segment returns no selection. The legacy weighted
selector uses visibility and continuity and returns none for ambiguous candidate tracks; segment
mode does not implement equivalent ambiguity rejection.

**Answer: the current selector answers A, “which track is easiest/best to analyze?” It does not
answer B, “which track belongs to the requested player?”**

| Potential identity mechanism | Finding | Classification |
|---|---|---|
| Manual point/bounding-box selection | No route/request-to-selector mechanism found. | absent |
| Face/jersey recognition, team classification, appearance Re-ID | No implementation found in selector/tracker/composition. | absent |
| Tracklet merging/cross-cut recovery | `NoOpTrackletStitcher` boundary only. | implemented but not production-wired for identity recovery |
| User visual seed | No request field or pipeline use found. | absent |
| Single-player eligibility / ambiguity rejection | Segment qualification exists, but it does not establish a requested player and has no dominance gate. | partially implemented |
| Continuity confidence | Segment continuity is an analyzability component; no identity-continuity state/confidence exists. | partially implemented |

The proposed distinctions `target_selection_status` (`ESTABLISHED`/`NOT_ESTABLISHED`) and
`identity_continuity_status` (`MAINTAINED`/`UNCERTAIN`/`LOST`/`NOT_EVALUATED`) are **Proposed**,
not current API/runtime states. Current automatic evidence can establish only that a visual segment
meets configurable quality thresholds; it cannot safely establish ownership by the requested
player, even for single-player videos, because no request-to-visual proof exists. Sprint 1 should
not address continuity/Re-ID, cross-cut recovery, or tracker redesign.

Target establishment is an initial eligibility decision, not real-world identity verification.
Identity maintenance is a later question about the chosen target over time; existing segment
continuity (`TrackSegment.continuity_ratio`) is only a ranking/analyzability input and must not be
renamed or represented as `identity_continuity_status`.

**Target established does not mean target maintained.** The intended automatic hierarchy is
**Proposed**: Request/input guarantee → Detection/tracking → qualifying visual candidates →
visual-target establishment eligibility → selected target segment → evidence → per-rating
eligibility → public response. Sprint 1 may implement only minimum initial target safety after
contract approval; it does not implement continuity, Re-ID, tracklet recovery, or tracker redesign.

Future manual-seed flow only is: Request → target-seed validation → detection/tracking association
→ visual-target establishment → continuity evaluation → evidence → rating eligibility.

## Documented decision — approved product input and provisional target policy

**Documented decision:** Apex associates `playerId` with a video captured/uploaded specifically to
analyse that player; other players may appear. This dedicated-video product guarantee supports
provisional selection but is not visual identity evidence and never proves `playerId == track_id`.

The approved conceptual method is `dominant_visual_candidate`: qualifying visual candidates are
ranked primarily by supported visible duration, with existing qualification evidence retained.
Top rank alone is insufficient; absent qualification or unresolved plausible alternatives yields
`NOT_ESTABLISHED`. Exact dominance thresholds remain **Unknown / requires verification**. This
decision is provisional selection, not biometric/jersey/real-world/persistent identity verification.

Continuity remains outside Sprint 1. Future fallback sequence: manual point, continuity gate, then
appearance Re-ID only if measured failures justify it. See [ADR-005](../../decisions/ADR-005-dominant-visual-target-mvp.md)
and the [proposed contract](../../contracts/target-selection-contract-v1.md).
