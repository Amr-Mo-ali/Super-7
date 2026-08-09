# 1. Executive Summary

Super-7 is a Python 3.12 FastAPI modular monolith that accepts one football video and returns one automatically selected tracked player, image-space movement/ball/event evidence, provisional scores, and an opt-in compact V2 response. Evidence: `src/main.py:create_app`, `src/api/routes.py:create_router.analyze`.

Current maturity: **MVP / production candidate for controlled internal use, not production ready**. The strongest parts are explicit quality gates, request admission, synthetic tests, immutable domain result models, and request-scoped artifact APIs. Serious risks are unconditional debug I/O/retention, a single-process capacity limit, detector/model runtime dependence, oversized response/diagnostic surface, and incomplete full-repository static verification. The latter is evidenced by CI requiring `mypy src tests` in `.github/workflows/ci.yml` and the current mapper’s extensive `object`-typed helpers in `src/api/public_rating_mapper.py`.

# 2. Product Goal and Current Scope

The intended problem is automated early-stage video evidence extraction for a selected football player. Input is exactly one multipart `video` file (`src/api/routes.py:create_router.analyze`); output is V1 analysis or opt-in V2 public rating (`response_version`). Consumers appear to be an internal dashboard/developer workflow (Inference: no authentication, tenant model, or user-facing product layer exists).

Supported: local video decoding, person/ball detection, ByteTrack tracking, target segment selection, candidate events, provisional physical/technical/rating indicators. Unsupported: persistent player identity, team/opponent/goal context, calibrated pitch/real-world performance, validated scouting/cognitive assessment, multi-camera/longitudinal analysis. Code broadly matches the stated MVP goal, but the README is behind the code: it describes tracking only and an intentionally empty package (`README.md`).

# 3. Repository Map

```text
src/main.py                         composition root (production)
src/api/                            HTTP orchestration and V2 presentation (production)
src/adapters/                       YOLO detector adapters (production)
src/services/                       tracking, video analysis, scoring, arbitration (production)
src/concurrency/                    process-local admission/executor/cancellation (production)
src/diagnostics/artifacts.py        request artifact lifecycle (production)
src/schemas/analysis.py             V1 Pydantic contracts (production)
src/schemas/public_rating_v2.py     V2 Pydantic contracts (production)
config/football_profiles.py         threshold profile data (production configuration)
tests/                              unit, API, integration, characterization tests (test)
docs/reviews/, docs/remediation/    architecture/remediation records (documentation)
.github/workflows/ci.yml            lint/type/test gate (tooling)
debug/                              generated request artifacts (debug-only/generated)
```

`src/football_analysis/` exists but has no feature code; it is packaging residue. `uv.lock` pins dependencies; `Dockerfile` builds a slim single-process image. No compose file or scripts directory exists.

# 4. End-to-End Runtime Flow

1. `src/api/routes.py:create_router.analyze` is async, accepts `UploadFile`, creates UUID, rejects extra form fields. It calls `temporary_upload`; it creates an OS temp file and always unlinks it.
2. `RequestLifecycle.execute_with_artifacts` synchronously reserves one process-local permit, creates `debug/<uuid>`, and uses `AnalysisExecutor.execute`/`asyncio.to_thread` for `_analyze_uploaded`. Finally it cleans artifacts unless retained and releases permit.
3. `_analyze_uploaded` validates metadata (`VideoValidator.validate`), then `DetectionOnlyPlayerTracker.analyze` decodes frames, uses YOLO person/ball adapters and ByteTrack. Outputs `TrackingRun`; detector failure becomes route failure or non-completed response depending on stage.
4. `WeightedTargetPlayerSelector` plus `segment_selection.build_segments/select_segment` selects one candidate. Failures return typed non-completed/ambiguous responses.
5. Ball reconstruction, proximity, movement, interaction, technical events, pass and shot detection run in the worker. Several scoring-stage exceptions are caught, logged, and leave unavailable evidence rather than aborting (`routes.py:_analyze_uploaded`).
6. Technical and physical scorers produce existing score results. V1 is assembled as `CompletedResponse`; V2 maps completed evidence through `EventArbitrator` then `GameIntelligenceEngine` in `api/public_rating_mapper.py:public_rating_v2`.
7. Side effects: source video is copied into artifacts and `render_debug_video` creates MP4 plus per-frame JPEGs. `ArtifactSession.retain` means normal cleanup retains them. Temporary upload cleanup remains owned by `temporary_upload`.

The actual order is scoring before V2-only arbitration/game intelligence; the V1 pipeline does not invoke `PlayerRatingEngine.summarize`.

# 5. Architecture and Layer Boundaries

Layers are mostly recognizable: adapters/infrastructure; analysis services; scoring/domain dataclasses; Pydantic/API; diagnostics; concurrency. Constructor injection in `main.py:create_app` is a useful composition boundary. Boundaries leak in `routes.py`, which is a very large orchestration and response-construction module, and `public_rating_mapper.py`, which converts Pydantic response schemas back into internal arbitration evidence. `Settings` mixes infrastructure, detector, analysis, score, debug, and pass thresholds. `football_profiles.py` adds an alternate dictionary ownership mechanism.

# 6. Major Components

| Component | Evidence / behavior | Keep/change assessment |
|---|---|---|
| Player detection/tracking | `adapters/yolo_player_detector.py`, `DetectionOnlyPlayerTracker.analyze`, `ByteTrackTracker`; decoded frames -> tracks/boxes. | Keep; model loading/thread safety needs operational validation. |
| Player selection | `services/selection.py:WeightedTargetPlayerSelector`, `segment_selection.py`; visibility/ball proximity and continuity gates. | Keep; thresholds profile-owned but duplicated in Settings. |
| Ball/reconstruction | `YOLOBallDetector`, `segment_ball.reconstruct`, `ball_tracker.py`; selected-segment ball points/quality. | Keep; image-space limits explicit. |
| Camera motion | `camera_motion.py:CameraMotionEstimator`; estimates frame transforms, but route diagnostics say raw image-space and no clear active route call was found. | Change: clarify or remove dormant path. |
| Movement | `movement/analyzer.py:BottomCenterMovementAnalyzer`; boxes -> image-space metrics. | Keep; not real distance/fitness. |
| Interaction | `interactions/analyzer.py:BallInteractionAnalyzer`; aligned ball/player evidence -> possible segments. | Keep; candidate rather than possession. |
| Controlled/dribble/loss | `technical_events/analyzer.py`; interaction/movement evidence -> candidate dataclasses. | Keep provisional gates. |
| Pass/shot | `pass_detection.py:PassDetector`, `shot_detection.py:ShotDetector`; independent trajectory candidate generation. | Keep; arbitration handles only downstream representation. |
| Technical/physical scoring | `scoring/technical.py:TechnicalScorer`, `scoring/physical_activity.py:RuleBasedPhysicalActivityScorer`. | Keep as explicitly provisional. |
| Player rating/game intelligence | `player_rating/engine.py`, `game_intelligence.py`; V1 adapter exists, while V2 directly evaluates GI. | Change: clarify one authoritative integration path. |
| Event arbitration | `event_arbitration/arbitrator.py:EventArbitrator`; immutable refs -> deduplicated/ambiguous timeline. | Keep; V0.1 only, document known limits. |
| V2 presentation | `api/public_rating_mapper.py`; compact dashboard transformation. | Change: strengthen typing and reduce stale helper duplication. |
| Diagnostics/debug | `schemas/analysis.py:Diagnostics`, `debug_renderer.py`. | Change urgently: response diagnostics are useful; default debug media is not. |
| Capacity | `AdmissionController`, `AnalysisExecutor`, `RequestLifecycle`. | Keep; process-local, no queue by design. |

# 7. Domain Models and Data Contracts

Frozen/slotted dataclasses dominate service boundaries: `PhysicalScoreResult`, technical-event models, interaction models, rating and arbitration models. Pydantic V1 contracts are concentrated in `schemas/analysis.py`; V2 in `public_rating_v2.py`. Versions are carried in result fields and `CompletedResponse.algorithm_versions`.

Risks: `Diagnostics` is a very large response model; several APIs use dictionaries (`algorithm_versions`, gates, evidence, quality, details), weakening static shape guarantees. V1 has `UnsupportedMetric`; other contracts use nullable values/reasons. This is mostly deliberate, but consumers must distinguish `None`, zero, `insufficient_evidence`, `unsupported`, `ambiguous`, and `unresolved_conflict`. `public_rating_mapper.py` has `object` helper parameters and legacy raw-event helpers after timeline introduction; this is weak typing and duplicated presentation logic.

# 8. Configuration and Threshold Ownership

`core/config.py:Settings` owns defaults for uploads (100 MiB, 900 s), detector paths/device, tracking, movement, interaction, technical events, scores, debug root, and pass rules. `Settings.from_environment` actually reads a subset: upload limits, model paths/device/confidence/IoU/image size, selection segment parameters, reconstruction parameters. Thus most Settings values are fixed code defaults, not environment-configurable.

`config/football_profiles.py:BALANCED_PROFILE` owns selection/ball/interaction/controlled/dribble thresholds accessed through `threshold`; conservative/aggressive profiles currently deep-copy balanced values, so profile names do not change behavior. `player_rating/config.py` owns GI weights/gates/cap; `event_arbitration/config.py` owns overlap/evidence thresholds. All alter public behavior and have focused tests for recent rating/arbitration thresholds. Hidden/magic defaults include hard-coded admission `1` in `main.py:create_app`, 30 fps conversions in obsolete mapper helpers, debug root `debug`, and retention default `None`.

# 9. File and Artifact Lifecycle

`temporary_upload` creates a named OS temporary uploaded video using client suffix, streams 1 MiB chunks, closes/unlinks on success/failure. `ArtifactManager.create_session` creates `debug/<analysis-id>`, path-validates UUID-style IDs, stages `*.partial`, quotas each session at `max_upload_bytes`, and can remove on cleanup.

However `_analyze_uploaded` always reserves/copies `source_video.<suffix>`, calls `artifacts.retain()`, and later calls `render_debug_video`. `debug_renderer.render_debug_video` creates `debug_video.mp4`, `debug_frames/`, and writes **every decoded frame** as `frame_######.jpg`. Its paths are inserted into `CompletedResponse.debug_artifacts`; therefore local absolute paths can reach V1 JSON. Since retained sessions are constructed with `retained_sessions=None` in `main.py`, successful and unsuccessful admitted analyses that reach `retain()` accumulate indefinitely. Cleanup handles ordinary completion/failure/cancellation finalizers, but not process crash; partial files may remain after crash. Request UUID prevents normal collisions. This adds a source copy, full decode/re-encode, and per-frame disk I/O to normal requests.

# 10. Debug and Observability Design

Application logging is standard-library named logging (`core/logging.py:get_logger`). The route logs validation, stage exceptions, and debug render failures. Diagnostics and timings are returned in V1; V2 hides most diagnostics. Debug MP4/JPEGs are developer artifacts, not production observability. `AdmissionMetrics` exists but no endpoint/export was found. Arbitration required structured events are not implemented: `EventArbitrator` contains no logger. Debug rendering is enabled by default and expensive; it is not a safe normal production default.

# 11. Concurrency, Capacity, and Request Isolation

`main.py:create_app` sets `AdmissionController(max_active_analyses=1)`. Admission rejects immediately (no queue); route returns a typed failed non-completed result, not HTTP 429. `RequestLifecycle` releases permit in `finally`; `ArtifactSession.cleanup` also executes in `finally`. Analysis runs in a worker thread via `asyncio.to_thread`. Cancellation is cooperative only; `_analyze_uploaded` immediately `del cancellation`, so detector/decoding work does not observe cancellation. State is request-local except singleton injected detector/tracker/services and ArtifactManager maps. Thread safety of YOLO/ByteTrack singleton use is not verifiable from repository, though capacity one limits overlap. No shutdown hook was found.

# 12. Error Handling and Failure Contracts

`VideoValidator` raises `InvalidVideoError`; route maps `AnalysisError` to HTTP 422 with message. unexpected exceptions are logged and mapped to 500 generic detail. Admission and unconfigured detector return non-completed `failed` responses. No players/valid tracks/selection are typed non-completed or ambiguous results. Individual ball/movement/interaction/event/scoring failures are often caught in `_analyze_uploaded`, logged, and represented unavailable. Debug rendering failure becomes warning. V2 arbitration is called without a local failure boundary; mapper failure would become generic 500. Artifact cleanup errors are deliberately non-throwing and are not visibly logged by lifecycle. Stack traces are not public, but V1 debug artifact paths are.

# 13. Testing Strategy and Coverage

Tests are organized by service plus `tests/api`, `tests/concurrency`, `tests/diagnostics`, and integration files. They use synthetic dataclasses, lifecycle/artifact tests, detector-free contracts, scoring golden regression, video I/O safety, and concurrency tests. CI sets intentionally invalid offline model paths and runs all quality commands, demonstrating tests should not invoke production inference (`.github/workflows/ci.yml`). Recent `test_game_intelligence_engine.py` and `test_event_arbitration.py` cover deterministic rules. Gaps: V2 full serialization/timeline compatibility test file is absent, route-level artifact default/retention behavior lacks an explicit policy test, cancellation does not test interruption of a running analysis, and no real performance/load/cleanup-after-crash test exists.

# 14. Performance and Resource Usage

Expected dominant cost is OpenCV decode plus YOLO inference per frame and ByteTrack; route timing captures stage milliseconds in `PipelineTiming`. The normal request additionally copies the full upload, re-decodes it for debug rendering, encodes an MP4, and writes one JPEG per frame. This is a high I/O estimate based directly on `debug_renderer.render_debug_video`, not a measured benchmark. Capacity one serializes analysis and prevents GPU/CPU contention but lowers throughput. Full trajectories in V1 pass/shot candidates and large diagnostics increase serialization/memory cost.

# 15. Security and Privacy Review

Positives: accepted suffix allow-list, streaming size limit, validated dimensions/FPS/duration, server-generated request IDs, artifact basename/path checks, staged writes, no arbitrary source path input. Risks: MIME/content is not trusted but suffix is client-controlled; decoding remains a CPU/resource DoS surface; default retained copies and annotated frames contain user video and may expose local paths through V1; no authentication/authorization or retention/privacy boundary is present. Logs use analysis IDs but may log exception context. No compliance claim is verifiable.

# 16. Production Readiness Matrix

| Area | Current state | Evidence | Risk | Severity | Required before production | Can be deferred |
|---|---|---|---|---|---|---|
| Correctness | Candidate pipeline tested | service tests/CI | heuristic outputs | high | acceptance fixtures | calibration |
| Determinism | Mostly explicit rules | scoring/arbitration tests | detector nondeterminism unknown | medium | reproducibility test | — |
| API stability | V1/V2 schemas | `schemas/*` | V1 internal paths | high | artifact separation | redesign |
| Concurrency | one permit | `create_app` | no scale/timeout | high | operational limit/429 policy | queue |
| Cleanup | temp robust, artifacts retained | lifecycle/artifacts/routes | unbounded disk/privacy | blocker | default-off/TTL | external store |
| Debug artifacts | always on | `_analyze_uploaded` | high I/O/leak | blocker | policy/cleanup | richer UI |
| Logging | basic named logs | `core/logging.py` | no metrics/events | medium | structured essentials | platform |
| Testing | broad synthetic suite | `tests/` | V2/lifecycle gaps | high | add contract tests | real corpus |
| Security/privacy | basic validation | `temporary_upload` | retained media | blocker | retention/access policy | formal program |
| Configuration | centralized but mixed | Settings/profile | hidden defaults | medium | document/expose policy | profile tuning |
| Deployment | Docker/CI exist | Dockerfile/CI | model asset/runtime unresolved | high | image/model/runbook | orchestration |
| Monitoring/rollback | not present | no endpoints/hooks found | poor operations | medium | basic health/metrics | platform |
| Documentation | remediation history | docs | README stale | medium | update product/runbook | exhaustive guide |

# 17. Findings

1. **F-001 — Default debug media is retained indefinitely.** Category: reliability/security/performance. Severity: blocker. Evidence: `routes.py:_analyze_uploaded` calls `artifacts.retain`; `main.py:create_app` supplies no retention count; `debug_renderer.py:render_debug_video` writes video and all frames. Every normal analysis consumes disk and retains user imagery. Action: explicit opt-in and bounded cleanup. Behavior change: yes. Test: retention/default-off/cleanup tests.
2. **F-002 — V1 exposes local artifact paths.** Category: security. Severity: high. Evidence: `render_debug_video` returns `str(Path)` and `_completed` assigns it to `CompletedResponse.debug_artifacts`. Action: remove from public contract or emit opaque IDs. Behavior change: yes. Test: response contains no local absolute paths.
3. **F-003 — Cancellation is not consumed by pipeline.** Category: reliability. Severity: high. Evidence: `routes.py:_analyze_uploaded` executes `del cancellation`; `CancellationManager` is cooperative. Action: add bounded cancellation checks between stages. Behavior change: no successful result change. Test: cancellation releases permit and stops before later stages.
4. **F-004 — V2 mapper has weak/stale presentation code.** Category: maintainability/correctness. Severity: high. Evidence: `public_rating_mapper.py:_game_evidence`, `_arbitrated_event`, and old `_controlled/_pass/_shot` use `object`; event timeline replaced older grouped helper flow. Action: type against domain/public models and remove unreachable duplicate helpers after compatibility review. Behavior change: intended no. Test: exact V2 serialization.
5. **F-005 — Configuration ownership is split and partially inert.** Category: architecture. Severity: medium. Evidence: `Settings`, `football_profiles.py` identical profiles, hard-coded `max_active_analyses=1`. Action: one documented policy surface. Behavior change: potentially. Test: environment/profile contract.

# 18. Debug Artifact Recommendation

| Option | Value / cost / suitability |
|---|---|
| A Always video + all frames | maximum diagnostics, unacceptable disk/I/O/privacy; not suitable. |
| B Disable all | safest but harms incident investigation; too blunt. |
| C Explicit request | high value, low normal overhead; suitable. |
| D Failure-only | useful but misses successful false positives; complementary only. |
| E Sample frames | useful low-cost supplement; suitable when opted in. |
| F Video without frames | less inode/I/O pressure; suitable opt-in default. |
| G temporary TTL cleanup | essential; current manager can support bounded retained sessions but not time TTL. |
| H external export | not justified for this MVP. |

Recommend C + F + G: default debug disabled; independently opt into video/sampled frames; request-scoped opaque artifact IDs; bounded retention count/storage budget and cleanup; no local paths in responses. Retain failure-only capture only where capacity permits.

# 19. Recommended Target Design

Keep the modular monolith. Add an explicit immutable debug policy at composition root and pass it to orchestration.

```text
HTTP request -> validation/admission -> worker analysis -> typed response
                                      |                 |
                                  optional artifacts <- debug policy
                                      |
                           request directory -> bounded cleanup -> opaque reference
```

Keep analysis dataclasses independent. Keep V2 as a one-way mapper. Separate operational diagnostics from public response, and centralize policy values without changing detection algorithms.

# 20. Remediation Roadmap

| Phase | Objective / likely files | Risk/tests/exit | Effort |
|---|---|---|---|
| 0 | Stop default artifact retention and public path leakage: `main.py`, `routes.py`, schemas, lifecycle tests. | behavior change; test no artifacts by default/no paths; exit: bounded cleanup. | small |
| 1 | Debug artifact lifecycle/policy: diagnostics, renderer, artifact tests. | medium migration; test opt-in, failure, quota, retention. | medium |
| 2 | Configuration/contracts: `core/config.py`, profiles, mapper/schemas, README. | compatibility review; exact V1/V2 tests. | medium |
| 3 | Verification: V2 fixture serialization, cancellation stage tests, load/timing characterization, CI clean mypy. | no product change; exit: reproducible green CI. | medium |
| 4 | Optional labelled-event calibration and field/goal context. | product/scientific work; defer until data. | large |

# 21. Keep / Change / Remove / Defer

| Keep | Reason |
|---|---|
| Constructor composition and protocols | testable modular monolith boundary. |
| Quality-gated provisional outputs | honest missing-evidence behavior. |
| Artifact staging/path validation | useful safe foundation. |
| Admission permit finally-release | simple correct process-local protection. |

| Change | Reason |
|---|---|
| Default artifact policy | current normal path is costly and retains media. |
| V2 mapper typing | dictionary/object handling weakens contract safety. |
| Settings/profile ownership | defaults and profiles are split/inert. |

| Remove | Reason |
|---|---|
| Unreachable legacy V2 raw-event helper code after compatibility confirmation | duplicates timeline presentation logic. |
| Empty `src/football_analysis` package if packaging does not require it | no feature responsibility. |

| Defer | Reason |
|---|---|
| Queues/databases/microservices | no demonstrated product need. |
| ML arbitration/calibration | labelled data absent. |
| Goal geometry/tactical intelligence claims | current inputs cannot support them. |

# 22. Open Questions

- Not verifiable from the repository: deployment topology, persistent volume policy, model asset provisioning, and authentication.
- Not verifiable: real-video latency, disk growth, GPU memory, and concurrent model safety.
- Not verifiable: intended artifact consumers and privacy/retention requirements.
- Not verifiable: whether V1 debug paths are intentionally an internal-only contract.

# 23. Final Recommendation

Freeze feature expansion briefly and implement Phase 0 next: make debug artifacts default-off, bounded when requested, and absent from public paths. Do not add distributed infrastructure, model training, or broader football-intelligence claims next. Debug saving is not currently acceptable for production normal traffic. The branch is mergeable only for controlled internal MVP use if the existing CI is green; it is not safe to label production-ready until F-001/F-002 and focused V2/cancellation verification are resolved.
