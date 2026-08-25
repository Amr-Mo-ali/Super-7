# Super-7 engineering instructions

## Mandatory onboarding and evidence guardrails

Before changing code, read the canonical handoff in this order:

1. [docs/handoff/README.md](docs/handoff/README.md)
2. [docs/handoff/scoring-and-product-semantics.md](docs/handoff/scoring-and-product-semantics.md)
3. [docs/handoff/system-and-runtime.md](docs/handoff/system-and-runtime.md)
4. [docs/handoff/decisions-progress-and-backlog.md](docs/handoff/decisions-progress-and-backlog.md)
5. [docs/handoff/production-evidence-and-operations.md](docs/handoff/production-evidence-and-operations.md)
6. Relevant ADRs, contracts, and runbooks.

- Do not fill null scores without new evidence.
- Do not equate event confidence with skill.
- Do not include game intelligence in Overall without a deliberate product and formula decision.
- Do not change process count without measurement.
- Do not introduce durability infrastructure as an incidental refactor.
- Do not assume `playerId` identifies a visual track.
- Do not store diagnostic evidence inside the production Git checkout.
- Do not deploy from feature/documentation commits unless explicitly intended.

## Scope and layout

Super-7 is a Python modular-monolith that analyzes football video and delivers an asynchronous callback. Core code is in `src/api`, `src/services`, `src/domain`, `src/core`, and `src/schemas`; tests are in `tests`; decisions and remediation records are in `docs`.

## Commands

Run from the repository root:

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run pytest -q
```

Use `uv run ruff format .` only when formatting is an intended change.

## Boundaries

- Super-7 owns video analysis, evidence, provisional ratings, and callback delivery. Apex owns its backend/product concerns; do not change Apex code from this repository.
- Preserve public contracts and callback behavior unless the task explicitly changes them. Add or update tests for every behavior change.
- Keep detector/tracker, scoring, API/presentation, and infrastructure concerns separated. Record material architectural or scoring decisions in `docs/decisions`.

## Evidence and scoring

- Treat executable code and tests as the authority for current behavior; validate documentation claims against them.
- `null` is not zero and is not automatically a failure: it represents unsupported or insufficient evidence according to status/reason.
- Never fill a null score, add a score, or claim calibration, football ability, fitness, pass completion, finishing, or tactical intelligence without documented evidence and validation.

## Delivery

Done means the change is scoped, backward-compatible unless explicitly approved otherwise, tested, linted/formatted/type-checked as applicable, and documented when it changes architecture or scoring semantics. Avoid over-engineering: do not introduce Kubernetes, microservices, infrastructure, or scoring features without a demonstrated requirement.
