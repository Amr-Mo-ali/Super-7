# Repository architecture map — Phase 1

Inspection date: 2026-08-04. This review is static: no production video inference was run.

## 1. Repository layout

```text
.
├── src/
│   ├── main.py                         # FastAPI composition root; module-level app
│   ├── api/
│   │   └── routes.py                   # POST /analyze orchestration and response mapping
│   ├── adapters/
│   │   ├── yolo_player_detector.py
│   │   └── yolo_ball_detector.py
│   ├── config/
│   │   └── football_profiles.py        # named threshold profiles
│   ├── core/
│   │   ├── config.py                   # Settings dataclass and environment loading
│   │   ├── exceptions.py
│   │   ├── logging.py
│   │   ├── pipeline.py
│   │   └── reproducibility.py
│   ├── schemas/
│   │   └── analysis.py                 # Pydantic public response models
│   └── services/
│       ├── video_validator.py
│       ├── player_detector.py / player_tracker.py / tracker.py
│       ├── ball_detector.py / ball_tracker.py / ball_proximity.py
│       ├── selection.py / segment_selection.py / segment_ball.py
│       ├── camera_motion.py / trajectory_compensation.py
│       ├── movement/{analyzer.py,schemas.py}
│       ├── interactions/{analyzer.py,confidence.py,models.py,segment_builder.py}
│       ├── technical_events/{analyzer.py,models.py,protocols.py}
│       ├── pass_detection.py / shot_detection.py
│       ├── scoring/{technical.py,physical_activity.py,models.py,protocols.py,level_mapper.py}
│       ├── feature_extractor.py
│       ├── debug_renderer.py
│       └── tracklet_stitching.py
├── tests/
│   ├── test_*.py                       # unit/API tests grouped mostly by module
│   └── integration/                    # camera, pass, shot, stabilization tests
├── config/                             # present but no files enumerated by rg
├── dataset/{raw/social_media,annotations}/
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── .env.example
├── .dockerignore / .gitignore / .pre-commit-config.yaml
└── README.md
```

`src/**/__pycache__` is present in the working tree, including stale-looking
`src/football_analysis/**/__pycache__` files without corresponding source modules.

## 2. Entry points

- **ASGI application:** `main:app`; Docker starts `uvicorn main:app --host 0.0.0.0 --port 8000`.
- **Application factory:** `main.create_app(settings=None, tracker=None, selector=None)`.
- **HTTP endpoint:** `POST /analyze` registered by `api.routes.create_router`.
- **Tests:** pytest runs `tests/` with `src` on `pythonpath`.

## 3. Composition root and construction flow

`main.create_app` creates `Settings.from_environment()` unless a test injects settings.
It constructs:

```text
YOLOPlayerDetector ─┐
ByteTrackTracker ───┼─> DetectionOnlyPlayerTracker
YOLOBallDetector ───┘
Settings ─────────────────────────────────────────────┐
                                                        ├─> router dependencies
WeightedTargetPlayerSelector(Settings)                 │
NormalizedBallProximityAnalyzer(Settings)              │
BottomCenterMovementAnalyzer(Settings)                 │
BallInteractionAnalyzer(Settings)                       │
TechnicalEventAnalyzer(Settings)                        │
PassDetector(Settings)                                  │
ShotDetector()                                          │
RuleBasedPhysicalActivityScorer(Settings)               │
FeatureExtractor()                                      │
VideoValidator(Settings)                                │
```

`tracker` and `selector` are injectable at the application-factory boundary; all
other services are constructed directly. `app = create_app()` is evaluated during
`main` import.

## 4. `/analyze` request flow

1. `routes.analyze` validates multipart fields and allocates an analysis UUID.
2. `temporary_upload` writes the upload; `VideoValidator.validate` reads metadata.
3. `AutomaticPlayerTracker.analyze` decodes frames, invokes supplied player/ball
   detector adapters and `ByteTrackTracker`, and returns `TrackingRun`.
4. The source is copied to a per-analysis debug directory and reproducibility
   metadata (hash/profile/commit/model version/timestamp) is captured.
5. Segment mode calls `build_segments`, `rejection_diagnostics`, and `select_segment`;
   otherwise the weighted selector ranks tracks.
6. Empty detections/tracks/selection return `NonCompletedResponse`; close rankings
   return `AmbiguousResponse`.
7. `_completed` scopes observations to the selected segment, estimates camera motion
   from the saved source, and runs segment ball reconstruction when applicable.
8. It runs ball proximity, movement, pass, shot, interaction, technical-event,
   physical-scoring, and technical-scoring stages. Most stage exceptions degrade
   individual outputs rather than fail the request.
9. It maps results to Pydantic `CompletedResponse`, adds diagnostics/timings/version
   data, runs `debug_renderer.render_debug_video`, then checks diagnostics invariants.

## 5. Responsibilities by module

| Concern | Primary modules |
|---|---|
| Video loading/upload/validation | `services.video_validator`, OpenCV in `player_tracker`, `camera_motion`, `debug_renderer` |
| Player detection | `adapters.yolo_player_detector`, `services.player_detector` |
| Ball detection | `adapters.yolo_ball_detector`, `services.ball_detector` |
| Tracking | `services.player_tracker`, `services.tracker`, `services.ball_tracker` |
| Target selection | `services.selection`, `services.segment_selection` |
| Segment ball reconstruction | `services.segment_ball` |
| Camera motion/compensation | `services.camera_motion`, `services.trajectory_compensation` |
| Movement | `services.movement.analyzer` |
| Interaction | `services.interactions.*`, `services.ball_proximity` |
| Technical events | `services.technical_events.analyzer` |
| Pass candidates | `services.pass_detection` |
| Shot candidates | `services.shot_detection` |
| Scoring | `services.scoring.technical`, `services.scoring.physical_activity`, `feature_extractor` |
| Response serialization | `schemas.analysis`, mapping helpers in `api.routes` |
| Debug video | `services.debug_renderer` |

## 6. Dependency graph

```text
main
 ├─ adapters (YOLO) ───────┐
 ├─ services/tracker ──────┼─> services/player_tracker ─> TrackingRun
 └─ Settings ──────────────┘                                  │
                                                             routes
                                                              ├─ segment_selection / selection
                                                              ├─ segment_ball / ball_proximity
                                                              ├─ camera_motion
                                                              ├─ movement
                                                              ├─ interactions
                                                              ├─ technical_events
                                                              ├─ pass_detection / shot_detection
                                                              ├─ scoring + feature_extractor
                                                              ├─ debug_renderer
                                                              └─ schemas.analysis
```

Most services depend downward on `core.config.Settings` and domain dataclasses.
`api.routes` is the primary integration point and imports nearly every subsystem.

## 7. Suspected circular dependencies

No direct source-level circular import was observed in the inspected files.

Potential pressure points, not confirmed cycles:

- `api.routes` performs local import of `TrackingRun` inside `_completed`, likely to
  avoid a heavier import edge.
- `debug_renderer` imports pass/shot result models; any future reverse dependency
  from pass/shot services to rendering would create a cycle.
- `feature_extractor` is a response-adjacent service; keeping it independent of
  `schemas.analysis` is important to avoid a `routes → feature_extractor → schemas`
  feedback edge.

## 8. Largest source files/classes

Largest Python source files (excluding bytecode):

1. `src/api/routes.py` — 50,961 bytes; route orchestration and response mapping.
2. `src/services/technical_events/analyzer.py` — 34,146 bytes; `TechnicalEventAnalyzer`.
3. `src/schemas/analysis.py` — 18,391 bytes; public API schema set.
4. `src/services/interactions/analyzer.py` — 13,356 bytes; `BallInteractionAnalyzer`.
5. `src/core/config.py` — 11,652 bytes; `Settings`.
6. `src/services/shot_detection.py` — 10,636 bytes; `ShotDetector`.
7. `src/services/pass_detection.py` — 10,441 bytes; `PassDetector`.

## 9. Highest apparent complexity

- `api.routes._completed`: long, stateful orchestration with segment scoping,
  optional-stage failure handling, timing, diagnostics assembly, and response mapping.
- `TechnicalEventAnalyzer.analyze`, `_controlled`, `_dribbles`, `_losses`: multiple
  candidate-generation and diagnostic branches.
- `PassDetector.analyze` / `ShotDetector.analyze`: possession grouping, release,
  trajectory, receiver/preparation/follow-through logic and rejection counting.
- `CameraMotionEstimator._estimate_interval`: feature extraction, flow, RANSAC,
  plausibility gating, and transform diagnostics.
- `BottomCenterMovementAnalyzer.analyze`: trajectory filtering and derived metrics.

## 10. Duplicated or similarly named concepts

- Two ball pipelines: streaming `ball_tracker` and selected-segment `segment_ball`
  reconstruction; both maintain continuity/gap/quality concepts.
- Proximity logic exists in `ball_proximity`, interaction analysis, pass detection,
  and shot detection.
- Trajectory calculations are separately implemented in movement, pass, shot,
  segment-ball, and debug rendering.
- Camera motion has a reusable compensator, but route wiring currently records camera
  diagnostics without passing compensated observations into movement/pass/shot stages.
- `DetectionOnlyPlayerTracker` is a misleading class name: it invokes a tracker and
  returns tracks despite the name.
- `debug_renderer` and the requested richer visual-debugging layer are distinct
  concepts; only `debug_renderer` exists as source.

## 11. Global state, caches, and import side effects

- `main.app = create_app()` constructs an application and detector/tracker objects on
  module import. Model adapters defer/own their model loading behavior; this needs
  runtime confirmation.
- `TechnicalEventAnalyzer` defines module-level `_LOGGER`; many modules define
  version constants.
- `Settings.from_environment()` reads process environment at factory invocation.
- `camera_motion` uses OpenCV and NumPy but no explicit global cache was observed.
- Route processing writes temporary uploads, copies source videos to `debug/`, writes
  debug videos/frames, and reads `.git/HEAD` for reproducibility metadata.
- `__pycache__`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, and `.uv-cache` are
  working-tree cache side effects.

## 12. Test organization and major gaps

Tests are primarily flat, one file per existing subsystem (`test_ball_pipeline`,
`test_segment_ball`, `test_interactions`, `test_movement`, scoring and API tests).
`tests/integration` contains deterministic synthetic camera-motion/pass/shot/stability
tests. Several requested category directories contain only `__init__.py`.

Observed coverage gaps:

- No dedicated tests for `debug_renderer`/artifact creation, source preservation,
  writer failure, or manifest/clip export.
- No end-to-end test with real detector/tracker adapters (appropriately absent from
  default unit tests, but leaves deployment validation open).
- No test proving camera-compensated coordinates affect movement/pass/shot metrics;
  compensation services are tested independently.
- No explicit tests for non-completed/ambiguous response timing, metadata, and quality
  gate consistency.
- No tests for concurrency, cleanup/retention of debug artifacts, upload-size pressure,
  corrupted debug output, or production configuration variants.
- No test coverage visible for `tracklet_stitching`.
- No migration/contract snapshot tests for the large `CompletedResponse` schema.

## Unresolved questions

1. Is `DetectionOnlyPlayerTracker` intentionally named for a former behavior, or is
   tracking meant to be optional in deployed configurations?
2. Are copied debug sources and generated artifacts intentionally enabled for every
   successful request, and what are their retention/authorization policies?
3. Which model-loading lifecycle is used by the YOLO adapters (per request, lazy
   singleton, or import-time), and what is the memory/concurrency budget?
4. Is camera compensation intended to become an input to event/movement calculations,
   or diagnostics-only at present?
5. Which historical `server-*.json` output corresponds to the intended production
   baseline, and are those fixtures meant to be maintained as regression tests?
