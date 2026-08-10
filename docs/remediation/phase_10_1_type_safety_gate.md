# Phase 10.1 — Type safety gate fix

## 1. Objective

Make the mandatory command below pass without weakening mypy, changing runtime behavior, changing public API semantics, or changing production algorithms, scoring, event arbitration, model ownership, concurrency, Docker, or infrastructure.

```text
uv run mypy src tests
```

## 2. Original mypy failures

The pre-change command reported nine errors in three test files:

| File | Error count | Mypy classification | Root cause |
| --- | ---: | --- | --- |
| `tests/test_public_contract_stability.py:124` | 1 | Unsafe Optional/Union access | `public_rating_v2()` returns `PublicRatingV2Response | PublicRatingV2Failure`; only the failure model has `reason_code`. The test accessed that attribute without narrowing. |
| `tests/test_event_arbitration.py:13` | 1 | Incorrect literal type | The fixture declared `event_type: str`, while `EventCandidateRef.event_type` requires `EventType`, the five-value `Literal`. |
| `tests/test_event_arbitration.py:27` | 6 | Dataclass `replace` typing mismatch | The fixture accepted arbitrary `**changes: object` and passed it to `dataclasses.replace()`. Mypy cannot establish that arbitrary object-valued keys are valid, correctly typed `EventCandidateRef` fields. |
| `tests/test_phase_00_safety_hardening.py:11` | 1 | Compatibility re-export typing issue / stale test import | `DebugSettings` is owned by `config.debug`; `core.config` imports it for `Settings` but does not explicitly export it. The test used the non-canonical import path. |

## 3. Files changed

| File | Change |
| --- | --- |
| `tests/test_public_contract_stability.py` | Imported `PublicRatingV2Failure` and narrowed the union with `assert isinstance(response, PublicRatingV2Failure)` before asserting `reason_code`. |
| `tests/test_event_arbitration.py` | Replaced the untyped `**changes` / `dataclasses.replace()` fixture with an explicit, typed fixture constructor. It accepts `EventType` and typed keyword-only `EventCandidateRef` fields. A private enum sentinel preserves the prior default receiver behavior while still allowing an explicit `None` receiver. |
| `tests/test_phase_00_safety_hardening.py` | Changed the `DebugSettings` import to its canonical owner, `config.debug`; kept `Settings` imported from `core.config`. |

No production source file changed. No mypy configuration changed. No `Any`, broad ignore, or `# type: ignore` was added.

## 4. Why the fixes are type-correct

### Public Rating V2 union

`public_rating_v2()` has an explicit union return contract. The test constructs `NonCompletedResponse`, so runtime produces `PublicRatingV2Failure`; the `isinstance` assertion expresses and verifies that precondition before the failure-only `reason_code` assertion. The assertion preserves the original runtime expectation and payload semantics.

### Event arbitration fixture

`EventCandidateRef.event_type` is defined as:

```python
EventType = Literal["controlled_movement", "dribble", "ball_loss", "pass", "shot"]
```

The fixture now uses `EventType`, preserving the production contract. Each supported override has the exact field type. This removes the invalid implication that every arbitrary `object` keyword can safely replace a frozen dataclass field. The sentinel distinguishes an omitted receiver from an explicitly supplied `None`, preserving the old fixture behavior: pass candidates default to receiver `9`, and non-pass candidates default to `None`.

### Debug settings ownership

`DebugSettings` is defined in `src/config/debug.py`. `core.config` consumes it to declare `Settings.debug`; it is not the canonical owner or an explicitly exported compatibility module. The test now imports from the owner already used by `tests/test_configuration_ownership.py`.

## 5. Runtime and public-contract impact

The changes are test-only and do not alter production code, serialization, API payloads, scoring, arbitration, tracking, model initialization, concurrency, or configuration behavior. The focused and full test results below confirm the existing runtime assertions remain true.

## 6. Verification

All commands were run after the changes.

| Command | Result |
| --- | --- |
| `uv run mypy src tests` | Passed: `Success: no issues found in 138 source files` |
| `uv run pytest -q tests/test_public_contract_stability.py` | Passed: 5 passed in 0.32s |
| `uv run pytest -q tests/test_event_arbitration.py` | Passed: 9 passed in 0.06s |
| `uv run pytest -q tests/test_phase_00_safety_hardening.py` | Passed: 5 passed in 0.58s |
| `uv run ruff check .` | Passed: `All checks passed!` |
| `uv run ruff format --check .` | Passed: `180 files already formatted` |
| `uv run pytest -q` | Passed: 210 passed in 4.08s |

## 7. Remaining typing risks

There are no remaining errors from `uv run mypy src tests` in this revision. This result establishes that the repository's configured strict mypy scope (138 source files) passes. It does not claim type safety for code outside that configured command or for runtime behavior that static analysis cannot model.

## 8. Exact next recommended phase

**Phase 10.2 — Docker deployment readiness remediation.** Address the pre-deployment report's Docker-only blockers, starting with a defined model-provisioning mechanism for the container image. This Phase 10.1 implementation deliberately did not change Docker, health endpoints, or shutdown/deadline behavior.

