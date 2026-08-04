# Phase 1.2 — ArtifactManager Implementation

## Scope delivered

Implemented a local, request-owned `ArtifactManager` and `ArtifactSession`. The
component owns only staged artifact creation, finalization, cleanup, retention, and
per-session quota reservation. It is not wired to debug rendering, routes, responses,
or the request lifecycle in this phase.

## Files created

- `src/diagnostics/__init__.py`
- `src/diagnostics/artifacts.py`
- `tests/diagnostics/test_artifact_manager.py`
- `docs/reviews/18_artifact_manager_implementation.md`

## Files modified

None.

## Design decisions

- **One session per request ID:** manager validation permits only safe alphanumeric,
  hyphen, and underscore IDs. Each session owns a directory directly beneath the
  configured root; duplicate active/existing directories are rejected.
- **Staged output only:** `create()` creates `<name>.partial`; only `finalize()`
  atomically replaces it with the final basename after size validation. `artifacts()`
  exposes finalized paths only.
- **Reservation-based quota:** each artifact reserves a non-negative byte budget; the
  session rejects reservations exceeding its configured aggregate limit and rejects a
  staged file larger than its own reservation.
- **Narrow paths:** artifact names must be one filename with no slash, backslash,
  traversal component, or nested path. The manager additionally resolves and verifies
  every request directory beneath its root.
- **Non-throwing cleanup:** cleanup returns immutable `CleanupResult` error text rather
  than raising filesystem cleanup errors, so it cannot mask a caller's primary failure.
  Repeated cleanup is harmless.
- **Deterministic retention:** `retain()` marks a finalized session for preservation.
  At cleanup, the manager keeps only the newest configured number of retained sessions
  by monotonically assigned completion order and removes older request directories.
- **Partial cleanup for retained sessions:** staged files are always removed during
  session cleanup, even when finalized outputs are retained.

## Invariants

- Each reservation carries its owning request ID and is accepted only by its exact
  session object.
- Partial files are never returned by `artifacts()` and cannot be finalized until
  created, present, and within quota.
- Session cleanup closes once and is idempotent thereafter.
- Filesystem cleanup errors are recorded rather than raised.
- Request IDs and artifact basenames cannot traverse outside the configured root.
- Aggregate reservation and actual per-artifact file-size limits are enforced.
- Retention order is independent of wall-clock timing and is deterministic under the
   manager lock.

## Risks

- This is process-local filesystem ownership only; there is no cross-process locking,
  distributed retention, object storage, database, or cloud backing.
- Reservations protect declared/session-local output sizes; external writers must use
  manager-created staged paths and finalize them for size verification to apply.
- Cleanup failures are deliberately non-throwing, so production integration must record
  `CleanupResult.errors` through its existing observability boundary.
- A later rendering integration must preserve the current public debug-artifact
  response contract and avoid exposing internal paths.

## Tests added

`tests/diagnostics/test_artifact_manager.py` contains deterministic coverage for:

1. successful creation and finalization;
2. cleanup execution and repeated cleanup;
3. cleanup following a primary failure;
4. partial-artifact rejection;
5. deterministic retention pruning;
6. quota exhaustion;
7. filename/path-traversal prevention;
8. concurrent request-session isolation.

Tests use `TemporaryDirectory`, `Path`, and standard-library threads only. They do not
use OpenCV, FastAPI, YOLO, GPU work, real videos, network calls, renderers, or models.

## Verification results

```text
uv run pytest -q tests/diagnostics/test_artifact_manager.py  # 11 passed
uv run ruff check .                                           # passed
uv run ruff format --check .                                  # passed
uv run mypy src tests                                         # passed (95 source files)
uv run pytest -q                                              # 124 passed
```

## Final status

Phase 1.2 is complete as a focused artifact-lifecycle component. No algorithm,
threshold, score, detector, tracker, endpoint, response schema, or diagnostics content
was changed.
