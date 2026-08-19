# Super-7 engineering instructions

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
