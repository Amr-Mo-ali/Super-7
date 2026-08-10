# Phase 11 — Tests, CI, and Deployment Review

**Scope:** tests, `pyproject.toml`, `uv.lock`, Ruff, mypy, pytest, coverage,
pre-commit, Docker, CI, and the production command.

**Review constraint:** no production model inference was run. CI was added without a
model download or model-inference step.

## Files reviewed

- `pyproject.toml`
- `uv.lock`
- `.pre-commit-config.yaml`
- `.dockerignore`
- `.gitignore`
- `.env.example`
- `Dockerfile`
- `README.md`
- `src/main.py`
- detector adapters and test files under `tests/`

## Changes made

- Added `.github/workflows/ci.yml`.

The workflow runs on Ubuntu with Python 3.12, installs the exact locked dependency
set using `uv sync --frozen --all-groups`, then runs Ruff, the formatting check,
mypy, and pytest. It sets model paths to deliberately nonexistent CI-only names and
enables Ultralytics offline mode. The existing tests use fakes, synthetic videos, and
fake model outputs; CI does not invoke a detector model or download production
weights.

## Tooling and reproducibility assessment

### Strengths

- `requires-python`, Ruff's target, mypy's Python version, and the intended runtime
  all specify Python 3.12.
- `uv.lock` records exact package artifacts and hashes; `uv sync --frozen` makes the
  new CI dependency resolution reproducible.
- Ruff and mypy are configured in `pyproject.toml`; the standard commands are
  documented in the README.
- Pytest is configured with `src` on its import path and `tests` as the test root.
- Tests are predominantly deterministic and use in-memory values, fakes, temporary
  paths, and tiny synthetic OpenCV videos. Current API tests inject a fake tracker;
  adapter tests use fake model output. No test requires an external video or network
  model download.

### Critical

None found in the checked-in test and build configuration.

### High

1. **Docker builds do not use the lockfile.** The Dockerfile copies `pyproject.toml`
   but not `uv.lock`, then runs `pip install .`. The declared project dependencies are
   bounded ranges rather than exact pins, so image builds can resolve a different set
   than local/CI even when `uv.lock` is current. This breaks build reproducibility.

2. **The production container runs as root and has no health check.** The image has
   no `USER` directive, no unprivileged writable runtime directories, no `HEALTHCHECK`,
   and no documented orchestrator health probe. A compromise in the web process has
   unnecessary root privileges.

3. **There is no coverage measurement or enforcement.** The project has no coverage
   dependency, configuration, report, baseline, or CI threshold. Test count is not a
   proxy for important-path coverage, particularly for route error mapping, cleanup,
   and model-adapter failure paths.

### Medium

1. **The Docker image will be larger than the source service needs for basic HTTP
   startup.** `ultralytics` pulls a substantial runtime dependency graph, including
   PyTorch variants. The single-stage `python:3.12-slim` image has no build cache
   separation, no non-runtime dependency pruning, and no model artifact strategy.
   The image does not currently copy `yolo11n.pt`, but model acquisition at runtime is
   not documented or controlled.

2. **Pre-commit's mypy hook is not guaranteed to reproduce the project environment.**
   It uses the isolated `mirrors-mypy` hook with no additional dependencies. Imports
   such as FastAPI, OpenCV, and Ultralytics can therefore differ from the `uv`-managed
   environment. CI is now authoritative, but local pre-commit can be noisy or fail to
   provide equivalent type checking.

3. **The production command has no operational worker/time-limit policy.** The Docker
   command starts one default Uvicorn process with no documented worker count,
   proxy-timeout coordination, graceful-shutdown timeout, concurrency cap, or
   resource limits. Given synchronous video analysis, adding workers blindly can also
   duplicate model memory and worsen GPU contention.

4. **Environment validation is incomplete at the deployment boundary.** `.env.example`
   only documents upload size and duration. Settings parses selected values at startup,
   but invalid numeric environment values yield raw conversion exceptions; model paths,
   writable debug/artifact storage, upload-temp space, and production diagnostic mode
   are not preflighted.

5. **API tests have a working-directory side effect.** Completed analysis tests use
   the default `debug_output_dir`, so debug files can be written under the repository
   working directory. They are git-ignored, but test isolation would be stronger if
   each application fixture injected a temporary debug output directory or disabled
   debug rendering.

### Low

1. **Dependency declarations are intentionally ranged, not fully pinned.** This is
   acceptable for application metadata because `uv.lock` exists, but every supported
   install/build command must consume the lockfile. The new CI does; Docker does not.

2. **No test markers separate fast unit, integration, adapter, and optional model
   suites.** Existing integration tests remain quick and deterministic, but markers
   will become important as real media fixtures, performance checks, or model tests
   are added.

3. **Some tests mainly assert ranges/status rather than exact behavior.** The new
   scoring golden tests improve this for scores. Similar golden/contract coverage is
   still absent for complete HTTP error bodies, debug artifacts, and non-completed
   response diagnostics.

4. **Tracked runtime artifacts deserve repository hygiene review.** The repository
   includes a model weight file, sample dataset videos, and server response/log
   artifacts. They are not required by the current deterministic tests. Their size,
   licensing, retention, and accidental use in CI should be governed explicitly.

## Test isolation and coverage gaps

The test suite does not depend on user-local paths or remote media. Synthetic video
fixtures are created inside pytest temporary directories and released correctly.
Likely flake sources are platform-specific OpenCV codec availability and module-level
application construction, rather than timing or random assertions.

Important missing tests:

- clean-checkout CI proof that no `.pt` model file is created or opened;
- public HTTP error-envelope/status tests for every validation and internal failure;
- concurrent request/admission and cleanup-failure tests;
- Docker build, non-root runtime, and health-check smoke tests;
- API serialization tests that bound production diagnostics;
- coverage reporting, including a separately agreed threshold;
- version-matrix testing if Python 3.12 remains the only supported version versus a
  minimum supported version.

## Deployment assessment

The documented local production-style command is:

```text
uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

The Docker command uses the equivalent Uvicorn invocation. This is suitable for local
development or a controlled single-process deployment, not yet a hardened production
runtime. Before broad deployment, make the container lockfile-based and non-root;
define model delivery, writable volume ownership, artifact retention, an orchestration
health endpoint/check, process limits, and upstream body/time limits.

## Remaining risks

CI now verifies static checks and deterministic tests from the lockfile without model
inference. Docker and production invocation remain less reproducible and less
hardened than CI. Coverage, container runtime hardening, and environment preflight
are the main outstanding quality-system gaps.
