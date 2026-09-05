# Super-7 repository audit — 2026-09-04

Audit performed on 2026-09-05 local time (Africa/Cairo); filename retained as requested. This is a review and documentation deliverable, not a release or production certification. No runtime, test, configuration, dependency, workflow, or lockfile changes were made.

## Executive assessment

The repository implements a working asynchronous, single-analysis modular monolith with substantial deterministic test coverage. It does **not** yet establish trustworthy player assessment, durable delivery, or production capacity. Repair the external callback serialization first: the canonical model declares the agreed aliases, but the actual transport sends snake-case availability/confidence keys. Passing schema tests conceal this transport defect.

The reported completed/null database record has several possible explanations. Current code intentionally produces that shape after target rejection, and missing per-rating evidence can also leave scores null. Neither explanation is confirmed for this incident. The owner identified the manually deployed revision as `54c6d00aacc1edba0c458c67de359b6fbe1f9882`; Git proves it is the audit branch base and that every commit above it before this audit is documentation-only. The audited runtime source is therefore equivalent to the deployed Super-7 runtime source for the reported attempt. The wire defect is confirmed in that deployed source, but no correlated callback transport evidence or Apex persistence mapping is available, so its incident contribution remains unverified.

Other reproduced defects include interaction-count truncation destroying otherwise valid evidence, speed inflation across frame gaps, an inclusive dominance boundary rejected through floating-point subtraction, and cancelled queue entries never completing queue bookkeeping. Product semantics also remain unfinished: detailed pass/shot fields still encode event confidence, public Game Intelligence remains a heuristic, and Overall does not require Technical. These must not be presented as calibrated ability.

## Starting state and evidence rules

| Item | Recorded state |
|---|---|
| Checkout | `E:\super7` |
| Branch | `docs/ci-main-validation` |
| HEAD | `22c1f8d310d124e23ef3c43ee247568be1e2a2de` |
| Working tree / index | Clean: `git status --short`, `git diff --stat`, and `git diff --cached --stat` produced no changes |
| Configured upstream | `origin/main` |
| Local upstream / owner-confirmed deployed production SHA | `54c6d00aacc1edba0c458c67de359b6fbe1f9882` |
| Divergence | `git rev-list --left-right --count 'HEAD...@{upstream}'` → `4 0`; no fetch or remote-state assumption |
| Python | Existing `.venv\Scripts\python.exe`, CPython `3.12.13`; `python` was not found by `Get-Command` |
| Base interpreter | Existing UV-managed CPython beneath the local temporary directory; no installation/recreation |
| UV | `0.11.2`; used only for `uv --version` |
| Existing quality tools | pytest `8.4.2`, Ruff `0.16.1`, mypy `1.20.2`, Pydantic `2.13.4`, Ultralytics `8.4.114` |
| Governing instructions | Root `AGENTS.md`; tracked/visible file inventory found no nested `AGENTS.md` |

All runtime findings below concern **HEAD**, not uncommitted implementation. `git merge-base --is-ancestor 54c6d00aacc1edba0c458c67de359b6fbe1f9882 HEAD` succeeds, the merge base is that exact production SHA, and `git diff --name-status 54c6d00aacc1edba0c458c67de359b6fbe1f9882..HEAD` lists only `docs/workplans/ci-deployment-separation.md`. The four intervening commits each modify only that documentation path. The findings therefore also concern the deployed Super-7 runtime source for the reported attempt. Source and executable offline observations take precedence over historical prose. Severity: P1 = repair before broader release/product claims; P2 = concrete next repair or prerequisite for the affected operating mode; P3 = lower-impact deferred issue. No P0 incident cause was established.

No production APIs, callbacks, databases, SSH, Docker, GitHub settings, real model/video inference, load tests, or Apex messaging were invoked. Tests used injected transports/models, synthetic observations, and the existing tiny codec fixture. Historical response/log contents, `.env`, credentials, and dataset annotations were not printed or used as incident evidence.

## Coverage and exclusions

Inventory at HEAD: **323 tracked files**, including **104 runtime Python modules / 12,782 lines**, **70 test/support Python files / 9,057 lines**, two scripts / 290 lines, and three integration Python files / 39 lines. Python line counts include package initializers and blank lines. The 269-file Ruff formatting count also includes locally discoverable Python files; it is not the tracked runtime count.

Review method: module inventory, source tracing of every runtime subsystem, schema/formula/control-flow inspection, complete test-function/assertion inventory, focused fixture/assertion reads, offline suite execution, and small synthetic reproductions. Coverage is risk based: this is not a claim that every test assertion or historical document received an equally deep line-by-line review. No code-coverage percentage was measured.

| Area | Reviewed scope / result |
|---|---|
| API and schemas | `main.py`; all `api` modules; both analysis and V2 schemas; admission, mapper, parent/child callbacks, nullable/legacy shapes |
| Execution | All `concurrency` modules; queue, worker, process pool, child entrypoint/contracts/composition; cancellation, cleanup, health |
| Detection and target | Both adapters, detector contracts, tracker adapter, player tracker, dominant selection, legacy selection, segments, no-op stitching |
| Evidence and scores | Ball tracker/proximity/reconstruction; movement; interaction modules; technical events; pass/shot; arbitration; technical/physical/player/detailed/Game Intelligence scoring |
| Foundations | All `domain` timeline/sequence/transition/possession modules; compensation and camera estimator; unused versus wired consumers distinguished |
| Configuration and diagnostics | Core/config packages, profiles, limits, logging, reproducibility, artifacts, renderer, performance collector; `.env.example` only |
| Tests | All 70 files inventoried; fixture/network/model seams and relevant gate, lifecycle, schema, numerical and CI assertions inspected; all 412 collected cases executed |
| Packaging and operations | `pyproject.toml`, lockfile metadata, Dockerfiles, both Compose files, CI/deploy workflows, ignore/pre-commit files; no builds or deployment |
| Governing documentation | Canonical handoff in order, scoring/lifecycle/process/target ADRs, target/job contracts, design boundaries, runbooks and Sprint 1 progress/verification; historical material treated as history |
| Developer support | Both scripts and backend mock inspected; mock is not Apex implementation |

Separately inventoried, **not line-by-line audited**: 90 tracked Markdown files (governing/current documents selected above; old reviews/remediation are historical), 18 tracked `.log` files, five historical response `.json` files, 15 `.mp4` videos, three annotation `.csv` files, one tracked `.pt` binary, the dependency lockfile's full transitive graph, installed third-party code, caches and ignored generated assets. The local ByteTrack implementation was spot-read for lifecycle/association behavior only. No binary/model quality, license/CVE, dataset-label, private-payload, or external-network audit is claimed.

## Actual architecture and execution paths

```text
POST JSON /analyze
  -> Pydantic four-field request validation
  -> safe filename syntax + synchronous public-DNS callback validation
  -> UUID analysisId + bounded memory queue (10 waiting by default)
  -> one AnalysisWorker; RUNNING
  -> parent builds safe ChildAnalysisRequest
  -> one spawn ProcessPoolExecutor child
  -> child path resolution + artifact session + video validation
  -> one tracker.analyze call: every decoded frame, person and ball inference
  -> target selection from unique per-track observed frame keys
  -> qualifying segment inside winning track only
  -> unavailable completion OR player-scoped evidence/scoring
  -> internal Pydantic response JSON + envelope
  -> child cleanup; parent validates schema/version/analysis ID
  -> parent arbitration/public mapping/detailed ratings
  -> callback transport, four attempts with 1/2/4-second waits
  -> terminal in-memory queue bookkeeping
```

Sources: `src/main.py:39–100`, `src/api/routes.py:109–180`, `src/services/analysis_queue.py:61–94`, `src/services/process_analysis_pool.py:71–126`, `src/services/process_entrypoint.py:76–118`, `src/api/routes.py:558–808`.

The CPU pipeline still lives inside the 2,167-line API `routes.py` module, imported by the child. This is an ownership/maintenance cost, not a reason to rewrite the architecture. `create_analysis_components` centralizes lazy construction. Parent component overrides do not change production child inference; process-pool injection is the active testing seam.

The alternate `create_analysis_job_processor` (`routes.py:392`) is retained/tested but not selected by `main.py`. It resolves the video, enters `RequestLifecycle.execute_with_artifacts`, obtains an admission permit, schedules a cooperative deadline, and invokes the same synchronous `_analyze_uploaded` through `asyncio.to_thread`. The production processor instead submits the child directly. Consequently the legacy deadline/admission counters are not production execution protection. The legacy downloader and old weighted/global-segment selectors remain code but are bypassed by `/analyze` and the current dominant resolver. `target_selection_mode` does not select an alternate production path.

Child artifacts are cleaned before the parent sends callbacks. A child cleanup error converts successful analysis to sanitized failure (`process_entrypoint.py:128–155`); a prior failure/cancellation is preserved. Legacy lifecycle cleanup **returned errors** are logged without replacing success, while a cleanup **exception** can replace success (`request_lifecycle.py:126–165`). This mode difference is real and documented here; it is not evidence that either occurred in the incident.

Callback failure/exhaustion normally leaves analysis `COMPLETED`, but the worker marks terminal state only after delivery attempts. Cancellation while waiting for delivery is different (F05). No result store, outbox, job query API, automatic restart recovery, idempotency key, durable cancellation, or separate callback dispatcher exists. The four-field API rejects the future job contract's added fields.

Shutdown closes admission and removes waiting jobs, then awaits legacy lifecycle shutdown, worker grace, and pool shutdown in sequence (`main.py:106`). Five seconds bounds the active worker grace only. `executor.shutdown(wait=True)` and subsequent thread/native completion are not bounded by that grace. No Compose `stop_grace_period` is declared; actual supervisor allowance is externally unverified.

## Target selection and identity truth

`playerId` remains business correlation only. A winning `track_id` is temporary, analysis-local visual evidence, never verified identity. `ESTABLISHED` does not establish `MAINTAINED`; there is no active continuity/Re-ID implementation or dedicated-video guarantee field. The approved dedicated-video guarantee is a caller/product precondition, not a visual identity signal.

`DetectionOnlyPlayerTracker.analyze` creates one tracker per analysis and calls its update once per decoded frame, including empty detections. `ByteTrackTracker._get_tracker` creates ByteTrack once and reuses it through that analysis (`tracker.py:80–132`). No first-party per-frame reset or synthetic fallback ID exists. Local dependency source increments its frame clock, associates detections, retains lost tracks for the configured buffer, and creates new IDs for unmatched detections. Fragmentation and same-ID switches remain possible; one visible person can therefore contribute multiple competing IDs. This is a plausible mechanism, not a diagnosis of this video. No threshold relaxation or Re-ID rollout is justified by the database row.

Current selection (`dominant_target_selection.py:44–198`):

- Unique `player_boxes` keys determine visibility count, count/processed-frames ratio, and count/FPS duration. Missing boxes or non-positive/non-finite FPS/processed-frame count fail qualification. Missing per-frame confidence later defaults to zero in segment evaluation.
- Track qualification requires ratio ≥0.20, longest contiguous run ≥5 frames, finite mean confidence ≥0.50. Continuity and mean confidence still come from track summaries. Structural key ranges/count consistency are not comprehensively validated at this boundary.
- Every other track with any unique observation and finite mean confidence is a plausible alternative, even if it does not qualify. Highest visibility wins only with an intended gap ≥0.08; F11 shows the exact-boundary defect. Sorting is deterministic by visibility then track ID; default positive margin rejects ties.
- All frames are decoded; there is no active sampling/stride. With common FPS and denominator, ranking by unique visibility is equivalent to supported duration. Frame-based qualification thresholds remain FPS dependent. Inclusive segment span/FPS includes tolerated gaps, whereas unique-frame/FPS duration excludes them.
- Segment splitting uses gap >3 missing frames or normalized center jump >3; eligibility requires ≥30 visible frames, ≥1 second inclusive span, mean confidence ≥0.30 and quality ≥0.45. The selected segment is filtered to the winning track before ranking. No qualifying winning segment yields unavailable; a losing track never becomes fallback.
- The no-person/no-track early branches precede the dominant gate and retain legacy noncompleted statuses (F12). Otherwise unavailable targets bypass `_completed`, all player-attributed movement/interaction/event/scoring, and public V2 projection. Person and ball tracking have already run over the video; this is an early scoring exit, not an inference-free exit.

## Null-rating incident

Owner-supplied facts, **not independently observed**:

| Item | Supplied observation |
|---|---|
| Analysis ID | `9075fe26-7b22-43e0-acf6-cd258e28e694` |
| Video ID | `79dc55d9-52ea-49bc-a155-3416f9167cbb` |
| Database state | `COMPLETED`; Overall, Overall confidence, all exposed ratings and error message null |
| Timestamps | `2026-09-03T21:18:58.849` → `2026-09-03T21:19:45.705`; timezone not supplied |
| Video context | Approximately 25.1 seconds, 1906×1080, 30 FPS; sampled images reportedly show one prominent player |
| Deployed Super-7 SHA | `54c6d00aacc1edba0c458c67de359b6fbe1f9882`; manually deployed exact SHA, owner supplied and consistent with the repository deployment record |

The timestamps differ by 46.856 seconds. That interval is database wall time with unknown boundaries, not a measured inference time or a basis for 3–5-minute latency extrapolation.

Git-verifiable source equivalence: the deployed SHA is an ancestor and exact merge base of audit HEAD `22c1f8d310d124e23ef3c43ee247568be1e2a2de`. Commits `d2331ef`, `bc1a726`, `9b4c767`, and `22c1f8d` above it modify only `docs/workplans/ci-deployment-separation.md`; the range diff contains no runtime, test, workflow, configuration, dependency, or lockfile path. This proves runtime-source equivalence, not dependency/image/model identity or what traversed the callback and Apex persistence boundaries.

| Path | What HEAD proves | Incident status / minimum discriminating evidence |
|---|---|---|
| Intentional target-unavailable completion | `routes.py:202–232,637–653,746–808` creates completed internal unavailable, null player/scores/summary; callback status `COMPLETED`, empty `ratings`, seven explicit detailed nulls, null Overall/confidence/error | Plausible. The deployed source is known, but the path taken is not. Need correlated `dominant_target_resolution` status/reason, then qualification counts/visibility/confidence/segments to assess whether rejection was justified |
| Missing per-rating evidence | Established targets still pass independent physical, interaction and technical gates. All core values can be null. Overall insufficient-evidence object then has `value=null`, internal confidence `0.0` | Plausible for ratings. Does **not alone** explain null confidence under a receiver that correctly persists the forwarded `0.0`; need exact field-presence/type and gate evidence |
| Serialization/alias mismatch | Actual sender omits `by_alias=True`; aliases become snake-case on the wire (F01) | Confirmed defect in the deployed source. Its contribution to this attempt remains unverified without correlated raw transport field-name/presence evidence and Apex mapping behavior |
| Callback projection loss | Unavailable builder intentionally empties ratings/events/summary. Available builder forwards ratings and Overall; no generic overwrite of populated scores found. It omits V2 warnings/quality/versions, and legacy failures lose their reason in callback projection | Need sanitized shape captured at mapper output and final transport, without private payload contents |
| Apex persistence mapping loss | No Apex DTO/controller/ORM mapping in this repository. Mock stores a dictionary by `video_id`; it does not map real rating columns | **Externally unverified**. Need deployed DTO alias rules, explicit null/absent handling, mapping of `overall.value` and `overallConfidence`, detailed-field mapping and transaction outcome |
| Incorrect lifecycle completion | Normal exceptions become failed callbacks; worker can still mark a returned legacy `NonCompletedResponse(status="failed")` completed, and callback-phase cancellation can disagree with receiver state | Possible only with matching execution evidence; no proof either was this incident |
| Lost callback / pre-existing backend transition | Exhausted callbacks do not change successful analysis state; Super-7 never writes this database | Need attempt outcome/HTTP class and Apex receiver/write correlation. A 2xx alone does not prove the intended columns were persisted |

Minimum remaining incident evidence package: deployed Apex SHA; sanitized correlated acceptance/start/target-resolution/execution/callback/terminal events; **field names, presence versus null, types, statuses/reasons, and bounded score values only** at sender and receiver; Apex DTO/controller/ORM mapping and persistence result. The database row alone cannot distinguish target unavailability, independent per-rating evidence gates, callback alias loss, or persistence mapping. If qualification or raw callback transport detail was not retained, explicitly record it as unrecoverable for this attempt rather than reconstructing it from screenshots. Do not request raw tokens, callback URLs, video contents, or private payload dumps. No production lookup was attempted.

## Public rating trace

All score values use 0–100; confidences use 0–1. Null and zero remain distinct.

| Public field | Evidence/formula at HEAD | Gate / meaning limitation |
|---|---|---|
| `technical_skill` | `scoring/technical.py:30`: equal average of available controlled/dribble **type means**, minus loss penalty capped at 0.25; controlled components 0.40 confidence +0.25 displacement +0.20 direction +0.15 duration; dribble components 0.30 confidence +0.25 movement +0.20 proximity +0.15 straightness +0.10 turns | Requires positive controlled/dribble candidates produced after technical quality gates. Confidence = quality × mean positive-event confidence. Candidate proxy, no calibrated skill/outcome |
| `physical_activity` | `scoring/physical_activity.py:22`: 0.35 intensity +0.25 active time +0.15 visibility +0.15 continuity +0.10 direction, ×100 | ≥3 seconds trajectory span, ≥30 observations, quality ≥0.55, visibility ≥0.20, accepted ratio ≥0.60; raw-image confidence cap 0.75. Pixels/image activity, not fitness |
| `ball_involvement` | `player_rating/engine.py:108–146`: 100 × min(1, (interaction duration + controlled duration)/5) | Coverage ≥0.60 and interaction count >0; confidence = coverage × interaction quality. Controlled intervals derive from interactions, so durations can overlap; this formula is an involvement proxy, not unique possession time |
| `overall` | `player_rating/engine.py:148–180`: available Technical/Physical/Ball weights 0.45/0.30/0.25, renormalized; ≥2 categories | Technical not mandatory. Confidence = mean category confidence × min(1,duration/5) × available/3. Duration is maximum evidence duration. Production excludes Game Intelligence; optional engine input is unsafe (F18) |
| `game_intelligence` | `public_rating_mapper.py:63–87,182–187`; five weighted components in `player_rating/game_intelligence.py:104–159`; ≥4 seconds and ≥3 components | Confidence includes duration, coverage, 0.75 missing-context factor; cap 0.65. No team/opponent/pitch/decision truth; remains public despite pending suppression decision |
| `detailed.speed_and_fitness` | `detailed_rating/engine.py:129–141`: physical evidence intensity ×100 | Requires provisional physical status and finite intensity; no independent fitness evidence |
| `detailed.ball_control_and_individual_skill` | Same per-candidate component formulas, averaged **per event**, loss denominator = positive event count | Positive technical quality and positive events; differs from Technical's per-type weighting/loss denominator when candidate counts vary; not one shared formula |
| `detailed.passing_and_playmaking`, `shooting_and_finishing` | `detailed_rating/engine.py:69–109`: mean finite confidence ×100 of arbitration-retained pass/shot candidates whose possessor matches selected track | Correct target filter here; quantity is event confidence, not pass quality or finishing. No detailed status/reason/confidence envelope |
| Other detailed fields | `DetailedRatings` defaults | Defending/duels, tactical/teamwork, positioning/off-ball have no runtime calculation and remain null in actual mapping |
| Other core fields | `player_rating/engine.py:15–23,222–234` | Soccer intelligence, tactical vision, mental stability, professionalism, growth potential, market readiness and scalability remain unsupported/null |

Pass/shot detection operates on all tracks within the selected time interval so receiver evidence is possible. Public event timeline/counts and Game Intelligence's event inputs are not filtered to the selected possessor, unlike detailed pass/shot scores. Treat those as interval observations unless a player-attributed contract explicitly filters them (F02). Neither event arbitration nor its confidence establishes football outcomes.

## Prioritized findings

### F01 — P1 — Actual callback serialization violates approved aliases

**Classification:** confirmed defect. **Sources:** `src/services/callback_service.py:33,48–53,142`; `src/api/routes.py:202–263`; `docs/contracts/target-selection-contract-v1.md:5–36`.

Every `send_result` calls `model_dump(mode="json")` without aliases. Synthetic injected transport captured `result_availability`, `unavailability_reason`, `overall_confidence`; the three approved camel-case names were absent. `player`, Overall, ratings and detailed values remain present. An alias-sensitive Apex DTO can lose availability/reason/confidence, making null results uninterpretable. High confidence in deployed-source wire behavior; contribution to this incident and receiver impact remain externally unverified.

**Smallest repair:** serialize canonical callback aliases at the one transport boundary; preserve legacy keys that have no renamed alias and explicit nulls. **Regression:** assert actual transmitted bytes for AVAILABLE and each UNAVAILABLE reason, zero versus null confidence, legacy success/failure, and parent/child-to-transport flow. Do not use the model's own default dump as the expected contract. Existing `tests/test_callback_service.py:13–22` does exactly that; alias tests explicitly enable aliases without exercising the sender.

### F02 — P1 — Public score meaning and Sprint 1 eligibility remain unfinished

**Classification:** contract gap (numeric proxy behavior confirmed). **Sources:** `src/services/detailed_rating/engine.py:46–67,69–180`; `src/services/player_rating/engine.py:75–180`; `src/api/public_rating_mapper.py:58–105,182–225`; target contract rating-gating section; target ADR rating consequences.

With accepted target pass/shot candidates, event confidence becomes a number in a skill-named detailed field. Physical intensity becomes `speed_and_fitness`. Public Game Intelligence persists without tactical evidence. Physical + Ball can yield Overall without Technical, and upstream numeric values can be relabelled available without checking their explicit status. Other players' interval pass/shot events can feed public summaries/Game Intelligence. These are not calibrated ability, and the proposed Technical-required Overall / public-null intelligence / event-skill separation is not implemented. High confidence in code; product migration agreement remains required.

**Smallest repair:** finish the existing product/contract decisions, then enforce explicit eligibility at rating/mapping boundaries; retain unsupported skill values as null and separate event observations. Preserve approved weights where eligibility is met; exclude Game Intelligence. **Regression:** Technical-null with Physical+Ball; rejected-status numeric upstream evidence; accepted event with unsupported skill; other-player event; unsupported detailed fields; established versus unavailable target. Existing tests asserting confidence-as-rating are characterization, not validation.

### F03 — P1 — Caller trust and video authorization are absent from the service

**Classification:** plausible risk; external perimeter unverified. **Sources:** `src/api/routes.py:132–180`; `src/schemas/analysis.py:17–25`; `src/main.py:120–159`; `docker-compose.yml:8–9`; `src/services/callback_service.py:222–231`.

Any caller who can reach the service can enqueue a known storage filename, choose business identifiers and a public callback destination. The repository has no authentication, per-video ownership check, caller scope or callback signature. This permits unauthorized compute and possible analysis-result disclosure if perimeter controls do not restrict callers. Compose publishes the port. No external reachability or exploit was tested.

**Smallest repair:** define and enforce the existing Apex integration trust boundary: authenticated admission, authorized video references, approved callback destinations and receiver authentication/replay policy. A narrowly restricted integration is sufficient; no new platform is implied. **Regression:** unauthenticated/unauthorized admission causes no queue entry; authorized requests preserve the contract; forged/replayed delivery rejected according to agreed policy. Verify firewall/proxy/Environment settings separately before wider activation.

### F04 — P1 — DNS checks do not bind the actual callback connection

**Classification:** plausible risk, supported by concrete control flow. **Sources:** `callback_service.py:130,142–148,211–232`; `routes.py:145`; legacy `video_downloader.py:96–137`.

The callback host is resolved and checked for global IPs, then urllib resolves it again at connection time. A DNS change between validation/attempts can reach an address never validated. Redirect rejection is useful but does not close this gap; proxy handling is also implicit. Resolver calls are synchronous on the API event loop, before admission and during delivery, without a DNS deadline. Slow DNS can block health/admission independently of model isolation. No live rebinding test was performed.

**Smallest repair:** constrain callbacks to the approved integration and bind connection address validation to each actual connection while preserving TLS hostname checks; offload/bound resolution. Keep redirects rejected. **Regression:** changing resolver answers/private second answer, mixed public/private answers, retry revalidation, proxy policy and a slow resolver that does not stall the event loop. Apply equivalent treatment to downloader only if that legacy path is reactivated.

### F05 — P1 — Callback-phase cancellation overwrites analysis completion

**Classification:** confirmed defect in terminal-state separation. **Sources:** `routes.py:326–330`; `analysis_queue.py:277–286`; `callback_service.py:147`; `tests/test_analysis_queue.py` callback-phase cancellation test.

After a successful calculation, shutdown can expire while the processor awaits callback delivery. Worker cancellation records `CANCELLED`, although analysis finished; the `to_thread` transport may continue and deliver a completed callback. Receiver and local terminal state can disagree. Existing tests establish cancellation bookkeeping but accept this semantic conflation. High confidence in the execution path; no incident attribution.

**Smallest repair:** record finalized analysis outcome independently before delivery and preserve it through cancellation; represent delivery interruption separately. This need not introduce a durable outbox. **Regression:** cancellation before analysis result, after result before callback, during transport after receiver acceptance, and after delivery; terminal analysis status must not be rewritten, callback behavior must remain explicit.

### F06 — P1 — Production execution has no effective deadline or unhealthy-pool admission gate

**Classification:** confirmed implementation gap; impact conditional on slow/hung/failed child. **Sources:** `main.py:74–100,135`; `process_entrypoint.py:76–127`; `process_analysis_pool.py:99–132`; `api/health.py:54–90`.

`REQUEST_DEADLINE_SECONDS` configures only the bypassed legacy lifecycle. The child token has no deadline or parent signal, and `await asyncio.wrap_future` has no timeout. A hung native call occupies the sole analysis slot and prevents total pool shutdown. A broken pool returns failures but stays installed; the worker keeps accepting/failing jobs. Readiness relies on unconditional `models_initialized=True` and a worker flag, not actual pool/model availability. High confidence; no hang/model failure induced.

**Smallest repair:** implement a bounded, explicitly defined process execution/degradation policy; at minimum stop admission and fail readiness after a broken pool or expired job, with an operator recovery path. A parent timeout alone must not advertise a released child capacity. Check local model files without inference. **Regression:** blocked fake child, broken pool on first/later request, absent model, no capacity double-use after timeout, and bounded supervisor escalation. Measure before choosing hard termination/recycling mechanisms or process count.

### F07 — P1 — Accepted jobs/results/delivery cannot survive restart

**Classification:** contract gap, explicitly accepted MVP limitation. **Sources:** `analysis_queue.py:73–86,134`; `callback_service.py:122–204`; ADR-004/005/006.

HTTP 202 records only memory. Crash/restart loses accepted jobs and results; graceful shutdown cancels waiting jobs without callback. Exhausted callbacks have no persisted retry/redrive. Repeated requests create unrelated UUID jobs and can produce duplicate or stale backend updates. No claim is made that this caused the reported completed row.

**Smallest repair:** before uncontrolled usage, agree unresolved-job retry authority/deduplication with Apex, then implement the already approved Super-7-owned acceptance/result/outbox design if accepted-job loss is unacceptable. Keep this a deliberate roadmap item, not an audit refactor. **Regression:** crash at acceptance/claim/result/delivery boundaries, duplicate submissions, stale attempts and callback replay. Do not increase workers as a workaround.

### F08 — P2 — Shared-file path bypasses configured upload-byte limit

**Classification:** confirmed defect. **Sources:** `video_validator.py:34–68`; `routes.py:584–595`; `video_downloader.py:79–92`; `core/config.py:15`.

Production validates file existence, metadata duration and minimum dimensions but never compares its size with `max_upload_bytes`. Only the inactive downloader enforces that limit. Synthetic metadata/capture with a two-byte file and one-byte limit was accepted. A large/high-resolution shared video can consume excessive decode/inference/hash resources; metadata duration is not an execution deadline. High confidence; no large media was processed.

**Smallest repair:** enforce existing byte limit before opening/decoding the shared file; define finite metadata and maximum decode dimensions/frame budget if resource requirements justify them. **Regression:** exact byte boundary, one byte over, invalid/non-finite timing, metadata-versus-decoded limits, and no detector work after rejection.

### F09 — P2 — Pass/shot speed overstates displacement across missing frames

**Classification:** confirmed numerical defect. **Sources:** `pass_detection.py:143–175`; `shot_detection.py:184–227,230–256`.

Release search compares a later observation with the old end-frame point but multiplies displacement by FPS without dividing by frame gap. Shot trajectory speeds do the same for tolerated gaps. Synthetic frames 0 and 6 at 30 FPS, four-pixel displacement: actual interval rate 20 px/s; both release routines report 120 px/s, crossing the default shot release threshold of 100. Wrong rates change candidate admission/confidence, then detailed scores. High confidence; candidate correctness in real videos remains unmeasured.

**Smallest repair:** calculate rates from actual positive frame/timestamp deltas; align acceleration with consecutive velocity intervals. Keep pixel units and thresholds unchanged pending measurement. **Regression:** contiguous/gapped equivalent motion, threshold below/at/above, invalid deltas and candidate confidence; do not treat fewer observations as faster motion.

### F10 — P2 — Interaction return cap destroys valid results

**Classification:** confirmed defect. **Sources:** `interactions/analyzer.py:111–155,268–279`; `core/config.py:121`; `routes.py:1472–1480`.

Accepted segments are truncated before accepted-count diagnostics are constructed, while raw/rejected counts describe the full set. With 101 valid segments and default cap 100, `_validate` raises `InternalInteractionDiagnosticsError: Interaction segment accounting must reconcile.` Synthetic 606-frame input reproduced this. Route catches the error and loses interaction/technical/ball-involvement evidence. More evidence can therefore cause fewer ratings. High confidence.

**Smallest repair:** distinguish total accepted evidence from returned detail count; preserve aggregates and account explicitly for truncation. **Regression:** 99/100/101 accepted runs, mixed rejected runs, configured small caps, and pipeline callback retaining valid aggregate evidence without exceeding response detail limits.

### F11 — P2 — Exact dominance margin can be rejected

**Classification:** confirmed defect. **Sources:** `dominant_target_selection.py:93–102`; `config/football_profiles.py:8`; `tests/test_dominant_target_selection.py:236–252`.

At 100 processed frames, winner 30 unique observations and runner-up 22, both otherwise qualified, subtraction yields `0.07999999999999999`. The strict comparison rejects this as ambiguous despite the intended inclusive 0.08 margin. Synthetic resolver probe reproduced `NOT_ESTABLISHED / ambiguous_visual_target`. High confidence; no evidence this exact boundary occurred in the incident.

**Smallest repair:** express the inclusive visibility-gap comparison robustly from counts/common denominator or a narrowly justified numerical tolerance. Do not relax the product threshold. **Regression:** 30/22 of 100 and equivalent fractions, one-count-below/above, ties and reordered candidates.

### F12 — P2 — Availability schema and legacy branches do not enforce one coherent external state

**Classification:** confirmed schema defect plus legacy contract gap. **Sources:** `callback_service.py:56–78`; `schemas/analysis.py:369–390,532`; `routes.py:609–627,241–263,327–330`.

The UNAVAILABLE callback validator prohibits populated player/Overall but accepts populated ratings and detailed values. A validated synthetic unavailable callback accepted Technical 88 and shooting 91. The current mapper is safe, so this is a missing boundary invariant, not proof of a present mapper leak. No-person/no-track results bypass the new availability surface and send legacy statuses with null availability and discarded V2 reason. A typed legacy `status="failed"` result can also be counted as completed by the processor.

**Smallest repair:** enforce all approved unavailable-rating invariants and explicitly decide compatibility for legacy noncompleted statuses; do not silently change externally supported statuses. **Regression:** start from a fully valid payload and vary exactly one contradiction. Existing contradiction test at `tests/api/test_callback_target_availability_contract.py:128–145` builds payloads missing unrelated required fields, so rejection alone does not prove the target invariant. Cover real no-person/no-track and typed failure through transport.

### F13 — P2 — Target rejection diagnostics omit evidence needed to explain it

**Classification:** confirmed observability defect. **Sources:** `routes.py:722–744,791–805,952,1032`; `dominant_target_selection.py:36`; `player_tracker.py:184–199`; `public_rating_mapper.py:149–168`; `routes.py:2162`.

The active resolution event contains status/reason/count and selected segment only. It omits winner/runner-up visibility, qualification failures and segment rejection detail. Rich `_log_target_track_evidence` and `_log_target_track_candidates` helpers have no production call sites. Unavailable diagnostics call every track a valid candidate; tracker lost/switch counters do not measure switches; `lost_track_count` is observations outside the longest run. V2 quality labels are hardcoded available/good, and technical-scoring gate is always accepted at 1.0 even when score is null. These statements can mislead investigation; V2 quality itself is dropped by the callback mapper.

**Smallest repair:** emit bounded sanitized resolver evidence for rejection and accurate gate/count/version labels, derived from existing data. No payload dumps or diagnostic store inside the checkout. **Regression:** real resolver call-site logging for ambiguous/no-qualifying/no-segment outcomes, logger failure isolation, and unavailable stage labels. Reconcile emitted diagnostics with code rather than testing helper functions alone.

### F14 — P2 — Debug mode bypasses artifact quotas and retains whole frame sequences

**Classification:** confirmed resource-control defects, opt-in path. **Sources:** `routes.py:603–608,1281–1289,1938–1954`; `debug_renderer.py:32–118`; `camera_motion.py:65–85`; `artifacts.py:43–93,136–183`; `core/config.py:220–270`.

Only the source copy is quota-reserved; debug video and per-frame JPEGs are written directly outside reservation/finalization. Camera estimation stores every selected grayscale frame and decodes the rest of the video too; its capture lacks exception-safe release. Retention bookkeeping is process-local, so old retained directories are not discovered after restart. `DEBUG_OUTPUT_DIR`, advertised by handoff/runbook, is not read by `Settings.from_environment`; a synthetic environment override still yielded `debug`. Default debug-off avoids these paths, but troubleshooting can create disk/memory failures precisely when evidence is needed.

**Smallest repair:** honor the documented output setting, bound renderer bytes/frames through artifact ownership, stream frame pairs, stop at segment end, release capture in `finally`, and define startup retention cleanup. **Regression:** quota exceeded by rendered media, conversion/read failure, environment override, restart retention and both save flags. Do not enable debug on longer videos before these limits are understood.

### F15 — P2 — Cancelled queue entries leave unfinished work; terminal history grows indefinitely

**Classification:** confirmed defects. **Sources:** `analysis_queue.py:73,107–129,256–267`.

`cancel()` sets state CANCELLED but leaves the queue item pending. Worker later skips it and calls `mark_finished`, which returns early because CANCELLED is already terminal, before `task_done` or timing removal. Synthetic submit→cancel→claim→finish left `wait_until_idle` timed out and retained one timing. This method is not exposed over HTTP, limiting current reach. Separately, all terminal `_states` entries are retained for process lifetime despite bounded waiting capacity.

**Smallest repair:** separate one-time queue accounting from terminal-state idempotence and bound terminal-history retention. **Regression:** cancellation before dequeue followed by queue join, mixed cancelled/completed FIFO, repeated finalization, shutdown after cancellation, and bounded state/timing growth.

### F16 — P2 — Reviewed source SHA does not identify the tested/deployed dependency artifact

**Classification:** confirmed reproducibility gap; remote controls externally unverified. **Sources:** `Dockerfile:1,11–16`; `.github/workflows/ci.yml:23–37`; `.github/workflows/deploy.yml:43–72,101–107,187–200`; `pyproject.toml:7–32`.

CI quality gates use `uv sync --frozen`; Docker instead installs range dependencies with pip and reinstalls headless OpenCV without `uv.lock`. Deployment rebuilds on the server, so the same reviewed SHA may produce a different dependency/image set. Model files are external mutable mounts; current versions mostly name paths. Wheel package selection also omits `main`, `adapters`, `config`, `concurrency`, `diagnostics` and `domain`; container source/PYTHONPATH masks that distribution gap. High confidence in build divergence; no image build was run.

**Smallest repair:** build from the locked runtime graph and promote/record an immutable tested image plus model identifiers; either complete wheel packaging or explicitly support only source/container execution. Preserve manual reviewed-SHA guards. **Regression:** CI/runtime dependency parity, clean installed-package import, image/SHA manifest, rollback to a previously tested artifact. Environment `production` is declared, but actual reviewers/protection, CI run evidence, host state and rollback execution were not queried. There is no automatic rollback after readiness failure.

### F17 — P2 — Legacy benchmark measures rejected upload requests, not current analysis

**Classification:** confirmed defect by API/control-flow inspection. **Sources:** `scripts/benchmark_analysis.py:18–43`; `schemas/analysis.py:16`; `routes.py:137`; `diagnostics/performance.py:134`; `scripts/process_pool_mvp_smoke.py:118`.

The benchmark posts multipart video to a JSON-only endpoint, does not enter application lifespan, does not await analysis/callback completion, and records non-success responses as timing samples. Even a request-shape fix would not carry its ContextVar collector into a spawned child. The smoke driver correctly measures admission only. Old benchmark output cannot establish full-analysis speed.

**Smallest repair:** adapt a staging-only driver to current JSON admission and correlated terminal/delivery evidence; instrument child stages explicitly and reject failed/empty runs. **Regression:** fake queued execution delays completion beyond admission, failed validation never counted as analysis, unavailable/full paths distinguished, child measurements returned and cold/warm labels correct. Do not run a capacity exercise as part of this audit.

### F18 — P2 — Optional Game Intelligence engine input crashes Overall

**Classification:** confirmed latent defect. **Sources:** `player_rating/engine.py:43–64,167–172`; ADR-001/002 mismatch sections.

Supplying an available Game Intelligence result plus one available core category causes `KeyError('game_intelligence')` because available categories enter a weight map with only three core keys. Synthetic Technical+Game Intelligence reproduced the exception. Production currently calculates Game Intelligence separately, so this does not explain the incident. High confidence; previously documented, still unrepaired.

**Smallest repair:** make Overall consume an explicit eligible core-category set; preserve exclusion and decide whether the optional summary field remains. **Regression:** available/insufficient optional intelligence with zero/one/two core categories; no weight change or accidental intelligence inclusion.

### F19 — P2 — Decoder early termination can become a misleading successful/unavailable result

**Classification:** plausible risk. **Sources:** `video_validator.py:40–57`; `player_tracker.py:89–112,165–172`; `routes.py:609–647`.

Validation decodes only the first frame. Tracking opens the file again and treats every failed read as ordinary end-of-video, without verifying successful open or reconciling decoded frames against expected metadata. A later I/O/decode failure or mutable shared file can therefore yield a partial run, legacy no-player outcome or target decision based on the truncated denominator. Actual malformed-video behavior was not exercised. High confidence in missing check; frequency/incident effect unknown.

**Smallest repair:** distinguish open/decode/truncation failure from accepted end-of-stream under a documented tolerance; retain decoded/expected counts and file identity, without assuming every container reports exact frame count. **Regression:** first-frame success followed by simulated early failure, reopen failure, changed file, and normal metadata rounding; failure must not be misreported as ordinary evidence scarcity.

## Verification record

Mandatory project commands are `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src tests`, `uv run pytest -q`. Existing interpreter entrypoints were used directly to avoid dependency synchronization/environment repair. UV was not invoked to install, sync or resolve dependencies.

A fresh external temporary root was created:

`C:\Users\ENG-AM~1\AppData\Local\Temp\super7-audit-b6777b61ee6144179375adb0d238a870`

`$auditTemp` below denotes exactly that root. No prior blocked artifact directory was selected as pytest base. There is no repository `tests/conftest.py`; test-local fixtures/support were inspected. No production model was loaded. Spawn verification uses the existing importable calculation fake.

| Exact command / operation | Result |
|---|---|
| `& .venv/Scripts/python.exe --version` | `Python 3.12.13` |
| `uv --version` | `uv 0.11.2` |
| `& .venv/Scripts/python.exe -m ruff check . --no-cache` | Exit 0; `All checks passed!` |
| `& .venv/Scripts/python.exe -m ruff format --check . --no-cache` | Exit 0; `269 files already formatted` |
| `& .venv/Scripts/python.exe -m mypy src tests --cache-dir "$auditTemp\mypy"` | Exit 0; `Success: no issues found in 174 source files` |
| `& .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider --basetemp "$auditTemp\pytest"` | Exit 0; **411 passed, 1 skipped in 12.19s** |
| `git diff --check` | Exit 0 before documentation; repeated after documentation |
| Synthetic Python probes through PowerShell here-string piped to existing interpreter | Nine observations recorded below; no network/inference |

Pytest process environment set `PYTHONDONTWRITEBYTECODE=1`, `ULTRALYTICS_OFFLINE=True`, `YOLO_CONFIG_DIR=$auditTemp\ultralytics`, `DEBUG_OUTPUT_DIR=$auditTemp\debug`. The last variable is **not honored by current Settings** (F14); it is not evidence that all internal test defaults were redirected. The fresh pytest base and explicit fixture paths are the verified test-output controls. Pytest stdout/stderr were captured in `$auditTemp\pytest.log` and only the result/skip summary displayed. No pytest warning section appeared.

Exact skip: `tests/test_video_path_resolver.py:48`, test `test_rejects_a_symlink_that_escapes_storage_root`; `[WinError 1314] A required privilege is not held by the client`. Symlink containment remains source-reviewed but not exercised on this Windows account. There were no failing pytest nodes and no tests were changed. Full suite was run once.

Ancillary limitations/errors, not hidden as passing checks: the initial `Get-Command python,uv` found only UV; trying to read nonexistent `tests/conftest.py` reported FileNotFound; an initial inline version probe lost shell quoting and raised `NameError`, then was rerun successfully through a here-string. `Get-CimInstance Win32_Process` was denied during the slow mypy run; mypy subsequently reported success. `Get-Date -AsUTC` is unsupported by this PowerShell version. None required an install/escalation or changed the audit conclusions. Large read outputs that truncated were followed by narrower source reads for findings; historical documents are not claimed exhaustively reviewed.

### Synthetic reproduction observations

These call production functions with manufactured data, not copied private payloads. Sources/inputs are sufficient to recreate the probes; they are not new committed tests.

| Probe | Input / observed output |
|---|---|
| Callback wire | Valid unavailable `CallbackPayload`, fake global resolver and byte-capturing transport returning 204; `send_result` emitted snake-case keys; `resultAvailability in body` was false |
| Schema invariant | Same valid unavailable payload with `ratings.technical_skill.value=88`, detailed shooting=91; validation accepted both |
| Queue cancellation | Capacity 1; submit/cancel/next_job/mark_running/mark_finished; `wait_for(wait_until_idle(),0.02)` timed out; `_timings` size 1 |
| Release speed | Box bottom-center `(0,0)`, height 100; ball `(0,0)` at frame 0 and `(4,0)` at frame 6; FPS 30; both `_release` methods returned 120 px/s instead of interval rate 20 |
| Interaction cap | Frames 0–605, 30 FPS, valid player box/confidence .9; ball at feet for five frames then far away for one, repeated 101 times; global qualities .9; accounting exception reproduced |
| File-size cap | Two-byte synthetic `.mp4`, injected metadata-only capture reporting 30 frames/30 FPS/640×480, configured byte limit 1; accepted file size 2 |
| Optional intelligence | `summarize` with Technical value 70/confidence .7 plus Game Intelligence value 70/confidence .5; `KeyError('game_intelligence')` |
| Dominance boundary | 100 processed frames, tracks with 30 and 22 unique frame keys, continuous summaries/confidence .9, FPS 30; ambiguous result at exact mathematical .08 lead |
| Debug output environment | `patch.dict(os.environ, {'DEBUG_OUTPUT_DIR':'audit-external'})`; `Settings.from_environment().debug_output_dir` remained `debug` |

## Deployment and security assessment boundaries

Confirmed workflow safeguards: manual `workflow_dispatch` only; full 40-character SHA validation; workflow must run from main; requested commit must be ancestor of locally fetched origin/main; successful completed CI matching SHA/main/push; validated outputs passed to SSH; clean server tree; detached checkout and HEAD equality before Compose mutation; production Environment declaration; serialized production deploys; bounded readiness attempts.

These prove repository workflow intent, not remote branch protections, required reviewers, settings, successful deployment, network isolation or runtime readiness. The workflow may deploy an approved ancestor, not only latest main; this is intentional reviewed-SHA/rollback capability. A failure after checkout/build/up has no automatic restore. The error message's nominal 60-second health window omits up to five seconds per curl attempt; it is not a strict total bound. Image healthcheck uses OpenAPI, which proves HTTP responsiveness rather than analysis readiness. Source capture and settings must identify actual deployed dependencies/models (F16).

Positive security controls: read-only model/video mounts in production Compose, non-root container user, filename containment and symlink resolution, public-IP checks and redirect rejection, sanitized normal child/parent failure envelopes, no callback URL crossing the child boundary. Limitations: unrestricted identifier strings are logged before stricter child validation, so control characters/oversized values can contaminate logs or fail only after admission; validate shared ID constraints before enqueue as part of F03. `logger.exception` in legacy/debug paths can include underlying filesystem/error detail; safe normal parent error logging does not justify a claim that all logs are sanitized. Secrets/payload logs were deliberately not inspected.

## Performance and later 3–5-minute benchmark

Confirmed execution costs: person and ball detectors independently load/cache models and call `predict` per frame, even when their paths are identical; batch method loops over single-frame calls. Tracking is consumed once, but this is **two detector inferences per decoded frame**, not shared inference. Native libraries may use multiple threads; no measured thread-cap policy exists. Tracking retains per-track boxes/confidences, observation lists and ball candidates until analysis ends, then serializes detailed results. Source hashing adds a full file read. Arbitration groups candidates pairwise; diagnostic Game Intelligence/arbitration runs in child and again for public mapping in parent. Debug adds copy/decode/render work and whole-frame retention (F14). None was benchmarked here.

Do not call these measured bottlenecks. The older VPS CPU/timing observations remain uncorrelated to an exact deployed revision and compare different videos; the now-known SHA for this reported attempt does not make those older measurements transferable. Do not extrapolate the 46.856-second incident database interval. Keep one process until measurement justifies change.

Later staging benchmark acceptance plan:

1. Fix/replace the obsolete driver (F17). Freeze reviewed source/image/model checksums, hardware, CPU/GPU device, thread settings, debug policy and video metadata. Agree acceptable latency/queue limits before running.
2. Use approved 3-minute and 5-minute samples at representative resolution/FPS, including established/full analysis, ambiguous target, no qualifying target, no player and sparse-ball cases. Record selection coverage/reasons so rejection is never counted as successful skill coverage.
3. For each **same video**, record fresh-child/model cold run, warm sequential repeats, and a controlled bounded queue scenario. Separate queue delay, model loading, validation/access/hash, decode, person detection, tracker update, ball detection/tracking, target/segment selection, reconstruction, movement/interactions/events/scoring, JSON/IPC, cleanup, callback attempts and true terminal end-to-end time.
4. Sample parent/child/container peak and steady RSS, CPU and native thread counts, GPU peak where applicable, disk/artifacts and handles. Existing `threading.active_count` is Python threads only; collector RSS on Windows is unavailable and Unix `ru_maxrss` is process-lifetime high-water, not per-job instantaneous memory. ContextVar state does not cross spawn.
5. Report per-case distribution and throughput from completed jobs over a defined wall interval, plus admission rejects, callback failures, unresolved jobs and coverage. Compare early-unavailable separately: it still decodes/infer-tracks the whole clip but skips downstream scoring. Do not publish supported-user numbers from request concurrency.
6. Verify responsiveness, cleanup and shutdown against explicit supervisor limits. Only then consider one measured two-worker experiment under ADR-006; no automatic count change or bigger model proposal.

## Roadmap reconciliation and historical corrections

| Capability | Verified at this HEAD | Deferred / unverified |
|---|---|---|
| Queue/process boundary | One in-memory FIFO worker; one spawn child; parent callbacks; process JSON validation | Durable jobs, idempotency, hard deadline/termination, restart recovery, pool recovery |
| Target establishment | Track-first unique evidence; plausible alternatives; winning-track-only segment; unavailable scoring bypass | Identity verification, maintained continuity/Re-ID, validated thresholds and acceptance/rejection rates; exact-margin repair |
| Availability integration | Internal unavailable schema, mapper, aliases declared and production target gate wired | Alias-correct transport, full unavailable schema invariant, legacy-status reconciliation and Apex persistence evidence |
| Ratings | Current technical/physical/ball formulas, per-evidence nulls, current weighted Overall, target filter for detailed pass/shot | Technical-required Overall, event confidence/skill separation, public intelligence decision, calibration/labelled football validity |
| Foundations | Timeline/sequence/possession value types, synthetic camera/compensation tests | Actual tactical/team/pitch/possession inference; compensated movement is not production scoring input |
| Operations | Health endpoints, worker grace, cleanup, scoped application logging, manual reviewed-SHA deploy guards | Whole-shutdown bound, meaningful pool/model readiness, immutable image promotion/rollback verification, remote Environment settings |
| Capacity | Deterministic offline tests and old supplied single-worker observations | Current 3–5-minute cold/warm/queue benchmark, throughput, resource limits and multi-user capacity |

Historical entries are preserved. Corrections explicitly applicable to current HEAD:

- Handoff snapshot `75c1682`, old segment-first discovery, and contract headings saying schema/mapper/pipeline “not implemented” are dated history. The gate/schema/mapper are now wired; transport aliases remain defective.
- Old design/ADR prose calling the process boundary “future/unused” or production thread-based is stale. The active composition is `create_process_analysis_job_processor`.
- “Game Intelligence excluded” describes the production call graph, not safe optional engine support (F18). “Evidence-gated” is not calibrated ability, and detailed confidence fields are still semantically problematic.
- Historical target-evidence helper tests do not establish current call-site logging. Rich helpers are uncalled, so do not promise those logs for this incident.
- General cleanup claims must distinguish primary failure preservation from cleanup replacing an otherwise successful child result, and distinguish worker grace from total shutdown.
- The old benchmark measures an obsolete endpoint; clean quality gates do not establish latency/capacity. Current manual SHA deployment supersedes historical fast-forward/latest-main deployment descriptions.
- Sprint 1 remains incomplete. Neither 411 passing tests nor workflow contract tests establish production readiness, ML quality, completed Apex integration or roadmap completion.

## Smallest ordered repair plan and acceptance criteria

1. **Fix callback transport aliases (F01).** One serialization-boundary repair with actual-byte regression tests across available/unavailable/legacy cases. Include fully valid contradiction fixtures for F12 if scoped independently. Acceptance: agreed fields and explicit nulls survive internal→child→mapper→sender; no score fabrication or formula change.
2. **Close the incident evidence boundary (F13 + external mapping).** Obtain the minimum sanitized correlated callback/Apex mapping evidence described above; add bounded rejection diagnostics for future attempts. Acceptance: distinguish intentional unavailability, insufficient rating evidence, transport loss and persistence loss; document unknown historical evidence honestly. The Super-7 deployed SHA is already established.
3. **Resolve and implement public eligibility/meaning (F02, F18).** Complete existing Sprint 1 decisions, then enforce core Overall eligibility and unsupported skill/intelligence behavior with compatibility tests. Acceptance: no event-confidence/fitness/identity claim leaks as validated skill; supported values remain deterministic under approved weights.
4. **Repair deterministic evidence defects (F09, F10, F11).** Separate small changes with gap, cap and margin regressions. Acceptance: time units use observed deltas, greater evidence does not trigger accounting loss, exact approved dominance threshold behaves inclusively.
5. **Repair execution safety before expansion (F03–F06, F08, F15, F19).** Establish authenticated integration/callback destination policy; bound admission I/O, byte/decode work and failed-pool behavior; separate analysis from delivery state and finish queue accounting. Acceptance: no unauthorized queueing, no silently healthy dead pool, no cancelled queue join leak or completed-analysis status rewrite.
6. **Make diagnostics/build/benchmark reliable (F14, F16, F17).** Bound debug resources and honor external paths; identify immutable tested deployment artifacts; run the explicitly approved staging benchmark. Acceptance: reproducible environment, useful stage/capacity evidence and explicit shutdown/rollback limits.
7. **Implement durability only at the agreed pilot boundary (F07).** Follow existing ADRs when loss/replay requirements demand it. Acceptance: crash/replay tests prove durable acceptance/result/delivery semantics. Do not add brokers, microservices or extra processes incidentally.

The single highest-impact next implementation task is **F01: fix and test canonical callback serialization at the actual transport boundary**. It is small, independently reviewable and directly affects the incident's observability without changing a score.

## Documentation delivery / Git state

Intended changes are this report plus append-only audit entries in `sprint-1/00-discovery-log.md` and `sprint-1/06-verification-results.md`. Existing historical entries are preserved. Before final-review mutation, no files were staged; this report was the sole dirty path and was untracked. Final-review authorization permits staging exactly these three documentation paths and creating one local documentation commit. No push, reset, stash, deployment, production access, GitHub API, SSH, Docker, load test, or real inference is part of delivery. Final scope/whitespace/link and production-to-audit runtime-equivalence checks are recorded in the appended verification entry.

## F01 tests-first transport contract — 2026-09-05

Source tracing reconfirmed the production path as internal `CompletedResponse` →
`routes._callback_payload` → `CallbackPayload` → `CallbackService.send_result` → HTTP transport
bytes. The schema declares `resultAvailability`, `unavailabilityReason`, and `overallConfidence`
aliases, but `callback_service.py:142` serializes with `payload.model_dump(mode="json")` and does not
set `by_alias=True`. Direct schema dumps with `by_alias=True` therefore do not prove the transmitted
shape.

The new focused red contract is
`tests/services/test_callback_transport_alias_contract.py`. Its nine collected nodes use the real
sender, a deterministic fake public-IP resolver, a byte-capturing successful transport, and a sleep
that would fail if a retry occurred. Coverage includes explicit AVAILABLE, all three approved
UNAVAILABLE reasons with no fabricated ratings, numeric-zero versus explicit-null confidence,
existing legacy success and failure shapes, and real available/unavailable mapper projections.
Parsed transport bytes are checked directly rather than compared with another model dump.

Current result: **7 failed, 2 passed**. Every failure reports only the three missing camel-case
aliases and the three unexpected snake-case forms; both legacy compatibility nodes pass. There was
no import, schema-construction, DNS, network, callback, retry, platform, or environment failure.
Existing callback tests are **12/12**, schema/internal-carrier tests **13/13**, and mapper tests
**6/6** green. The pre-existing offline suite, with the intentional-red module excluded, is
**411 passed, 1 skipped**; the skip is the documented Windows symlink-privilege case. An initial
full-suite invocation supplied a nonexistent `--basetemp` parent and ended at 384 passed / 28 setup
errors, all the same `FileNotFoundError`; creating the external parent and rerunning removed every
setup error. Focused Ruff check/format, syntax compilation, and mypy over `src` plus the new and
affected callback tests are green. The first test-only mypy command omitted `src` and consequently
classified local packages as untyped installed distributions; the repository-shaped rerun reports
no issues in 109 source files.

This phase changes no runtime. The approved smallest green change remains canonical alias
serialization at the single `send_result` transport boundary—equivalent to adding `by_alias=True`
to that model dump—while preserving explicit nulls, numeric zero, legacy fields, compact JSON, and
all current schema/formula behavior. F01 remains the highest-impact next implementation task and
Sprint 1 remains incomplete. These deterministic tests do not establish ML calibration, capacity,
incident causation, latency, or correct target rejection from null ratings.

## F01 local green phase — 2026-09-05

The audit finding remains historically accurate for committed/deployed source, but the current
uncommitted working tree now contains the approved smallest repair. At the single sender boundary,
`CallbackService.send_result` calls `payload.model_dump(mode="json", by_alias=True)` before the
existing compact `json.dumps`. No schema, mapper, route, score, retry, URL-validation, transport,
configuration, dependency, workflow, or lockfile behavior changed.

The pre-change contract was reconfirmed at **7 failed / 2 passed**, with only the three missing
camel-case aliases and corresponding snake-case keys. After the one-line semantic change, all nine
actual-byte nodes pass, including AVAILABLE, every approved UNAVAILABLE reason, explicit null versus
numeric zero, legacy forms, and real mapper projections. The existing generic callback test initially
failed because its oracle repeated the old default model dump; its single assertion now requests
aliases explicitly, while the independent transport contract continues to assert literal wire keys
and values.

Final implementation evidence is transport contract **9/9**, existing callback **12/12**,
schema/internal carrier **13/13**, mapper **6/6**, and full offline suite **420 passed / 1 skipped**.
Mypy is green across 175 source files; full Ruff lint/format and changed-file syntax compilation pass.
The skip remains the documented Windows symlink-privilege case. This is a local, uncommitted and
undeployed repair: it does not retroactively prove F01 caused the reported incident, change the
missing Apex/raw-transport evidence boundary, establish calibrated ML quality or capacity, measure
latency, or prove that null ratings mean correct target rejection. Sprint 1 remains incomplete.

## F01 final commit review — 2026-09-05

Human review accepted the six-path change set without further code changes. The sender diff adds only
`by_alias=True`; it does not add `exclude_none`, `exclude_unset`, or `exclude_defaults`, so explicit
nulls and numeric zero remain serialized. Retry delays, URL/DNS validation, redirect rejection,
timeouts, logging, transport and caught exceptions are untouched. The existing callback-test diff
changes only its expected model dump to request aliases. The nine-node transport contract uses the
real sender with an injected local resolver, byte-capturing transport and fail-on-use sleep; it does
not derive its alias expectations from the sender's default dump or depend on key order/source text.

Final-review verification: transport **9/9**, callback/schema **12/12**, internal unavailable plus
mapper **11/11**, parent/child plus process callback wiring **33/33**, and full offline suite
**420 passed / 1 skipped**. Both mypy scopes, full Ruff lint/format, affected syntax/imports,
Markdown links, trailing whitespace, diff checks and scope checks pass. The skip is only the known
Windows symlink-privilege case. References above to an “uncommitted” working tree accurately record
their earlier phase checkpoint; this reviewed set is the authorized local commit candidate and is
still undeployed. Apex persistence and the historical incident's per-field causes remain externally
unverified. Sprint 1, production readiness, rating calibration and capacity remain unestablished.
