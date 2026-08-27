> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Current implemented behavior

```mermaid
flowchart LR
 A[POST /analyze] --> B[validate request and safe video reference]
 B --> C[bounded in-memory FIFO queue]
 C --> D[one parent worker]
 D --> E[one spawned process-pool child]
 E --> F[detector/tracker → segment target → evidence/events → ratings]
 F --> G[parent validates serialized response]
 G --> H[parent builds callback and retries delivery]
```

| Component | Current behavior | Evidence | Classification | Sprint 1 impact |
|---|---|---|---|---|
| Request/admission | `AnalyzeRequest` accepts `videoId`, `playerId`, `videoUrl`, `callbackUrl`; extras forbidden. Route returns queued 202 with `analysisId`, video and player IDs. | `src/schemas/analysis.py`, `src/api/routes.py`, route tests | Implemented and production-wired | Player identity enters here but has no visual binding. |
| Queue/worker | Lifespan starts bounded `asyncio.Queue` and one `AnalysisWorker`; state is process-local and non-durable. | `src/main.py`, `src/services/analysis_queue.py`, API tests | Implemented and production-wired | No change proposed. |
| Child boundary | Spawned one-worker process pool owns CV composition; parent validates serializable child result. | `process_analysis_pool.py`, `process_entrypoint.py`, child tests | Implemented and production-wired | Target/rating gate must cross this boundary deliberately. |
| Video/CV | Child validates/resolves video and composes YOLO person/ball detection, ByteTrack, ball tracking and downstream pipeline. | `analysis_composition.py`, `player_tracker.py`, `routes.py` | Implemented and production-wired | Current tracking identity is analysis-local. |
| Target selection | Segment mode builds qualifying visual track segments and chooses highest segment quality. | `segment_selection.py`, route lines around selection, selection tests | Implemented and production-wired | It chooses analyzability, not requested-player identity. |
| Evidence/rating | Physical, technical, interactions, detailed ratings and V2 public rating mapping are computed after a selected track. | scoring/rating modules; mapper; rating tests | Implemented and production-wired | No gate connects target identity to rating availability. |
| Overall | Available technical/physical/ball categories are weighted and renormalized; at least two required. | `player_rating/engine.py`, `test_player_rating_engine.py`, ADR-002 | Implemented and production-wired | Can produce a number for an unverified visual target. |
| Callback | Parent owns payload construction and four-attempt callback delivery; delivery failure does not rewrite completed analysis. | `routes.py`, `callback_service.py`, callback/processor tests | Implemented and production-wired | Any public representation requires Apex compatibility review. |

The callback result is distinct from analysis state: analysis may be completed while callback
delivery exhausts retries. Failed analysis has a sanitized failed callback; cancellation has no
callback. `null` rating means unavailable/unsupported or insufficient evidence, not failed
analysis or zero (ADR-003). Current diagnostics include candidate/track/evidence quantities, but
not proof that the selected visual person is the Apex player.

Configuration affecting selection is owned by `Settings`: selection mode, gap, visible-frame,
duration, mean-detection-confidence, quality and normalized-centre-jump thresholds. Production
wiring uses the application composition above; Docker/CI/deploy wiring is documented in the
handoff and workflows. No live production behavior was exercised in this discovery.
