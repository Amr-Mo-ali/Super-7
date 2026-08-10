# Configuration and composition-root review — Phase 2

Review scope: settings, profiles, environment handling, startup, dependency construction,
module-level objects, and test replacement seams. No detection or analysis algorithm was
reviewed. No production video inference was run.

## Changes applied

None. The review found no clearly safe code change that would improve readiness without
altering lifecycle or configuration semantics. In particular, making model creation lazy
would move model-load failures from application startup to the first request, so it is not
treated as behavior-preserving.

## Findings

### Critical

None found in the reviewed scope.

### High

#### Importing `main` eagerly constructs and loads both YOLO models

- **Evidence:** `main.py` ends with `app = create_app()`. `create_app()` constructs
  `YOLOPlayerDetector` and `YOLOBallDetector` when no tracker is supplied. Both
  constructors use `model or self._load_model()`, and `_load_model()` invokes
  `ultralytics.YOLO(...)` synchronously.
- **Impact:** ASGI import, test collection paths that import `main.app`, and worker startup
  require model artifacts and the Ultralytics runtime. A model loading failure prevents
  app import/worker startup rather than yielding an application-level health failure.
- **Current mitigation:** tests that use `create_app(..., tracker=FakeTracker)` avoid this
  path. `tests/test_health.py` imports the module-level `app` and therefore does not.
- **Recommendation for a later lifecycle-focused phase:** explicitly decide whether models
  are startup-owned resources or first-request resources; do not silently change this in a
  configuration-only patch.

#### Settings are not Pydantic settings and lack central validation

- **Evidence:** `core.config.Settings` is a frozen dataclass. `from_environment()` performs
  direct `int()`/`float()` conversion for a subset of fields.
- **Impact:** malformed environment values raise raw conversion exceptions during startup;
  the majority of declared settings cannot be configured by environment; cross-field
  invariants (weights, non-negative limits, valid mode values, paths) are not centrally
  validated.
- **Pydantic verification:** no `pydantic-settings` dependency or `BaseSettings` subclass is
  present. Pydantic is used for response schemas only.

### Medium

#### Configuration has three sources of defaults

- `Settings` contains many literal defaults.
- `config/football_profiles.py` duplicates a subset of those defaults in
  `BALANCED_PROFILE`; `threshold()` supplies only profile-listed keys.
- `Settings.from_environment()` repeats literals for a smaller, different subset.

Examples include target-segment defaults duplicated between the dataclass, profile, and
environment parser. Model/ball values are duplicated between dataclass and environment
parser. Pass and debug values are declared only in `Settings`, and camera-motion values live
in a separate `CameraMotionConfig` rather than the runtime settings object.

#### Profile names do not currently represent distinct profiles

`CONSERVATIVE_PROFILE` and `AGGRESSIVE_PROFILE` are deep copies of the balanced profile.
`ACTIVE_PROFILE` is a module constant; it cannot be selected through environment variables
or application construction. The recorded profile name is therefore always `balanced`.

#### Constructor injection is uneven

`create_app` allows injection of `Settings`, `AutomaticPlayerTracker`, and
`TargetPlayerSelector`. The remaining services are directly constructed inside the factory
and cannot be replaced through the public factory signature. This limits integration tests
for movement, interactions, technical events, pass/shot, scoring, video validation, camera
motion, and debug rendering.

The router itself has explicit constructor injection for all those dependencies, which is a
good dependency direction. The composition root is the missing test seam.

#### Startup/shutdown ownership is implicit

No FastAPI lifespan handler exists. YOLO adapter ownership is implicit in Python process
lifetime; no adapter exposes a `close()` method. OpenCV resources in request processing are
mostly released locally, but app-level resources have no shutdown contract.

#### Debug output is an unconditional request-side effect

`routes.analyze` copies every uploaded video to `Settings.debug_output_dir` before knowing
whether analysis will complete. This is not controlled by an environment setting and has no
retention, cleanup, or failure-isolation policy at the composition-root level. It may cause
a successful analysis to fail if the debug directory cannot be written.

### Low

#### `.env.example` does not describe the actual supported environment surface

It documents only `MAX_UPLOAD_BYTES` and `MAX_DURATION_SECONDS`, while
`Settings.from_environment()` also reads model, ball, selection, segment, and stitching
variables. Conversely, many `Settings` fields are not environment-addressable.

#### Raw configuration values are logged

Startup logs model path and device. Detector logs model paths; no obvious secret-bearing
setting is declared or logged. There are currently no API-key/password/token settings in the
reviewed configuration surface. If future settings include secrets, current exception
wrapping (`f"...: {error}"`) and unstructured logs could expose them.

#### Module-level mutable state and caches

No application-managed mutable service cache was found. Module-level state consists mostly
of constants, profile dictionaries typed as `Final` but technically mutable, and named
loggers. The FastAPI `app` object and eagerly-loaded models are process-global objects.

## Dependency construction assessment

`main.create_app` is the single visible composition root and direct dependencies flow from
adapters/services into `api.routes`, not the reverse. `create_router` receives its runtime
dependencies explicitly and does not use a service locator. This is sound.

The root does create model-backed adapters directly and establishes module-level application
state via `app = create_app()`. The lack of a lifespan/resource-owner boundary is the main
startup concern.

## Test replacement assessment

- **Detectors/trackers:** yes, indirectly. `create_app` accepts a replacement tracker;
  `tests/test_analyze.py` injects `FakeTracker`. Detector adapter unit tests inject fake
  model objects into the adapter constructors.
- **Selector:** yes. `create_app` accepts a replacement selector.
- **Other analyzers/scorers/validator/debugger:** not through `create_app`; direct router
  construction would be needed. No factory-level parameter exists for them.
- **Import safety:** incomplete. `tests/test_health.py` imports `main.app`, so test
  collection/import depends on installed model artifacts and model-loading behavior.

## Exact modified files

- `docs/reviews/02_configuration_and_composition_root.md` — this review report only.

## Tests added

None: no behavior-preserving application change was applied.

## Remaining risks

1. Eager module loading makes ASGI import and test collection operationally dependent on
   model availability.
2. Invalid environment values fail without domain-specific validation errors.
3. Configuration is partially centralized and partially duplicated, risking drift.
4. Generated debug video copies have unclear ownership, retention, and failure behavior.
5. App-level resources do not have explicit startup/shutdown lifecycle ownership.
