> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Verification results

## Provenance and Git state

The **original inspected runtime baseline** was clean on `the-new-inhancement` at
`7920375b915e852486643df8eb5bf27bf8fb09ae`. The discovery documents were later committed as
`f2d1e834843bbdc542cc36bdbf05ef7f127fd617`. `git diff --name-status` over that range reports only
these eight added Markdown files under `docs/workplans/sprint-1/`; therefore no runtime, API,
formula, configuration, test, infrastructure, deployment, or CI behavior changed between the
baseline and documentation commit.

Although the original discovery task instructed no commit/push, the documentation was committed
and, per the supplied correction brief, pushed afterward. Local Git evidence verifies the commit
and its file-only range; it does not independently verify remote push receipt. History is retained,
not rewritten. The current working tree is a documentation-correction working tree based on that
commit; this correction changes only Sprint 1 Markdown files.

## Original discovery verification

Created documents: `README.md`, `00-discovery-log.md`, `01-current-behavior.md`,
`02-target-and-identity-discovery.md`, `03-rating-semantics-discovery.md`,
`04-contract-decisions-required.md`, `05-minimal-implementation-map.md`, and this file.

No CV/model inference, model download, live request, callback delivery, deployment, or
application-environment mutation occurred. Focused tests were limited to selector, ratings, public
contract and parent processor behavior; they did not start. `uv run` failed to initialize/query its
local cache/interpreter, and direct `.venv` invocation failed when its UV trampoline could not spawn
a Python child (permission denied). This is an environment limitation, not a passing or failing
pytest result.

The original `git diff --check` completed without reported errors but did not include then-untracked
new documents. A PowerShell check resolved every repository-relative Markdown link in the pack. No
repository Markdown checker was found in the inspected CI/workflow material.

## Correction-session verification

The correction session verified current HEAD, the baseline-to-documentation commit file list, and
the documentation commit message/date. It makes no runtime assertions beyond that immutable
baseline evidence. `git diff --check` completed without content errors; every repository-relative
Markdown link in the Sprint 1 pack resolves. `git status --short` reports only the eight modified
Sprint 1 Markdown files. Git emitted LF-to-CRLF working-copy warnings for those files; this is
recorded as a warning, not a validation failure.

The correction task started with these eight already-modified documentation files and ends with
the same exact scope: `00-discovery-log.md`, `01-current-behavior.md`,
`02-target-and-identity-discovery.md`, `03-rating-semantics-discovery.md`,
`04-contract-decisions-required.md`, `05-minimal-implementation-map.md`,
`06-verification-results.md`, and `README.md`, all under `docs/workplans/sprint-1/`. The captured
current `git diff --stat` reported these eight files only (246 insertions, 76 deletions before the
final verification-record edits). `git status --short` likewise reported only those files.

The trailing-whitespace scan (`rg -n "[ \t]+$" docs/workplans/sprint-1 --glob '*.md'`) found none.
The repository-relative link check found all links resolved. Available Markdown checks: no
Markdown-specific checker was found by searching `pyproject.toml`, `.github`, and documentation;
the repository's existing CI checks are Ruff, formatting, mypy, pytest and image build, not a
Markdown linter. No tests were reattempted in this documentation-only correction. The previous
focused pytest attempt remains **Unknown / requires verification** because UV/Python failed before
pytest started with permission errors; no tests are claimed to have passed.

No runtime code, API, schema, formula, test, configuration, infrastructure, deployment, CI,
commit, or push changed during this correction task. No CV inference or live request occurred.

Known limitation: ADR-001/002 describe a `KeyError` if available Game Intelligence is passed into
`PlayerRatingEngine.summarize`, while production mapping keeps it separate. No football validation
or live behavior was measured. All behavior proposed by this workplan remains labelled Proposed.

## 2026-08-29 contract-approval documentation pass

Starting branch and HEAD: `the-new-inhancement` at
`f8664cd3446324bb1507d8b98c7ddfaff32bc21f`. Starting working tree was clean; the older Sprint 1
correction work is contained in that documentation commit and was preserved. Created files:
`docs/decisions/ADR-005-dominant-visual-target-mvp.md` and
`docs/contracts/target-selection-contract-v1.md`. Modified files:
`docs/workplans/sprint-1/00-discovery-log.md`, `02-target-and-identity-discovery.md`,
`03-rating-semantics-discovery.md`, `04-contract-decisions-required.md`,
`05-minimal-implementation-map.md`, and `README.md`.

Commands executed (sanitized): full required document/source reads; `git branch --show-current`;
`git rev-parse HEAD`; `git status --short`; `git diff --stat`; `git diff --check`; an `rg` trailing
whitespace scan; a PowerShell repository-relative Markdown link check; and an `rg` search for
Markdown tooling. Results: branch/HEAD above; the final pre-record status contained those six
modified workplan documents plus the two untracked new Markdown documents; `git diff --stat`
reported six tracked workplan files (92 insertions, 7 deletions) and intentionally does not include
untracked files; `git diff --check` reported no content error; no trailing whitespace was found;
all repository-relative links resolved; no Markdown-specific tool was found in `pyproject.toml` or
`.github`. Git emitted LF-to-CRLF working-copy warnings for existing modified workplan files.

Tests were not edited or run: this task is contract/documentation-only and no runtime behavior was
tested or changed. The earlier pytest result remains **Unknown / requires verification** because
the environment prevented pytest startup. No CV inference, live request, callback delivery,
configuration change, deployment, commit, or push occurred in this pass.

Final static recheck at `2026-08-29T12:14:03.4634315Z`: `git diff --check` produced no content
error; the Markdown trailing-whitespace scan found none; repository-relative links resolved; and a
terminology/order search found the required non-identity and non-fitness caveats plus no stale
automatic target-before-tracking hierarchy. At that check, `git status --short` contained exactly
seven modified files under `docs/workplans/sprint-1/` (including this verification record) and the
two new Markdown files above. `git diff --stat` showed the seven tracked workplan files only (137
insertions, 17 deletions); Git does not include the two untracked new files in ordinary diff stat.

## 2026-08-29 dominance hotfix

Starting branch/HEAD: `the-new-inhancement` /
`fa84d18e4d4432a15941a1995c20707edd8b1971`; starting working tree was clean. Modified files:
`docs/contracts/target-selection-contract-v1.md`,
`docs/decisions/ADR-005-dominant-visual-target-mvp.md`, and
`docs/workplans/sprint-1/{00-discovery-log,04-contract-decisions-required,05-minimal-implementation-map,README}.md`.

Commands executed: `rg -n -i -S` for `yes by definition`, qualifying-candidate, dominance and
plausible-alternative terms; `git diff --check`; `rg -n "[ \t]+$"` for trailing whitespace; a
PowerShell repository-relative Markdown link check; `git status --short`; and `git diff --stat`.
Results: no unsafe automatic-establishment wording remains; all remaining single-qualifier wording
states the prohibition. `git diff --check` produced no content error; trailing whitespace was none;
all repository-relative links resolve. Final status contains exactly the six Markdown files above.
`git diff --stat` reports those six files: 49 insertions and 16 deletions. Git emitted only
LF-to-CRLF working-copy warnings. Runtime tests were intentionally not run; no runtime code, test,
API/schema, formula, model, infrastructure, deployment, commit, or push changed.

## 2026-08-29 implementation-discovery pass

Started clean on `the-new-inhancement` / `275141d7ed57215352c47a3e5d5c14a7d20fd89d`. Created
`docs/workplans/sprint-1/07-dominant-target-implementation-discovery.md`; modified the mandatory
activity and verification records and README link only. Source/test reads and Git inspection were
performed; no tests or runtime behavior were run.

Initial static checks: `git diff --check` and trailing-whitespace scan were clean; scope contained
only permitted discovery documents. The initial repository-relative link check found that new
source/test links used `../../` rather than `../../../`; this was corrected before final recheck.
No runtime/API/formula/model/infrastructure/deployment/commit/push action occurred.

Final recheck at `2026-08-29T12:52:03.8029344Z`: `git diff --check` produced no content error;
trailing whitespace was none; every repository-relative Markdown link resolves; and scope check
confirmed only `00-discovery-log.md`, `06-verification-results.md`, `README.md`, and new
`07-dominant-target-implementation-discovery.md` changed. `git status --short` reported those three
modified files plus the new untracked discovery document. `git diff --stat` reported the three
tracked files (17 insertions); the untracked document is not included in ordinary diff stat. Git
emitted LF-to-CRLF working-copy warnings only. Tests were not run.

## 2026-08-29 track-first implementation-discovery correction

At `2026-08-29T13:03:45.2879493Z`, source verification read the governing ADR/contract and
Sprint 1 records plus `PlayerTrack`, legacy selection, tracker summary/observation collection,
segment selection, route composition, settings/profile defaults, and `test_selection.py`.
`PlayerTrack` is constructed from tracker-returned observations; it carries visible observations,
processed-frame denominator, adjacent-frame longest run, derived lost count, and mean confidence.
The tracker list can retain duplicate same-frame observations while segment dictionaries overwrite
them. Default production wiring uses segment mode: all segments are ranked globally and the route
sets a one-item `ranked` tuple, so its later generic ambiguity check is unreachable in that mode.

The corrected discovery design is **Proposed**: select/determine dominance on `PlayerTrack`, retain
plausible alternatives for the policy decision, then rank qualifying `TrackSegment` evidence only
for the winning `track_id`. It explicitly requires no second tracking pass and a tie-safe future
`NOT_ESTABLISHED` result. It selects no thresholds and makes no runtime, test, API/schema,
callback, formula, setting, deployment, dependency, CV/live, commit, or push change.

Final static recheck at `2026-08-29T13:06:49.6791595Z`: `git diff --check` reported no content
error; the Sprint 1 trailing-whitespace scan found none; and every repository-relative Markdown
link resolved. The initial scope predicate incorrectly treated Git's allowed `??` marker for new
07 as out of scope; after correcting that predicate, scope contained only modified
`00-discovery-log.md`, `06-verification-results.md`, and `README.md`, plus untracked
`07-dominant-target-implementation-discovery.md`. `git diff --stat` reported tracked files only:
49 insertions and 4 deletions; Git emitted LF-to-CRLF warnings only. Tests were intentionally not
run because this is a documentation-only correction.

## 2026-08-29 tests-only phase — starting state

At `2026-08-29T13:15:29.0673238Z`, the tests-only brief, mandatory handoff, proposed ADR/contract,
all Sprint 1 workplans, relevant selection/tracker/segment/rating code, and focused test conventions
were inspected. Starting branch/HEAD were `the-new-inhancement` /
`275141d7ed57215352c47a3e5d5c14a7d20fd89d`. Starting status contained only the expected user-owned
Sprint 1 documentation changes: modified `00-discovery-log.md`, `06-verification-results.md`, and
`README.md`, plus untracked `07-dominant-target-implementation-discovery.md`; no runtime or test
file was changed. The proposed tests will deliberately import a missing internal policy module so
missing implementation is visible as an expected failure, not a skipped or falsely passing test.

Focused baseline, new-test, and combined test commands were attempted at
`2026-08-29T13:18:15.5944241Z`. They did not reach pytest: `uv run` failed to initialize
`C:\Users\ENG-AMR MOHAMMED\AppData\Local\uv\cache` (`os error 183`), and direct `.venv` execution
failed because the UV trampoline could not spawn a Python child (`os error 5`). Standalone `ruff`
and system `python`/`py` are unavailable, so neither lint/format nor syntax compilation could run.
This is an **Unknown / requires verification** environment blocker, not a pass, failure, or observed
missing-module import failure. No model/CV inference, network request, or runtime change occurred.

At `2026-08-29T13:19:47.0983730Z`, a retry with `UV_CACHE_DIR` outside the checkout still failed
to query Python under the sandbox (`os error 5`). An approved elevated retry reached the editable
build but failed in `hatchling.build.build_editable` with `ModuleNotFoundError: No module named
'typing'`. The suggested build-dependency workaround is not authorized for this tests-only phase,
so no dependency, environment, or runtime change was made. This is an unexpected blocker separate
from the deliberately absent dominant-target implementation; no pytest collection or expected
missing-module failure could be captured.

Final static/scope verification at `2026-08-29T13:20:21.2312202Z` found no `git diff --check`
error, trailing whitespace, broken repository-relative Sprint 1 link, or changed `src/`, schema,
configuration, dependency, lockfile, or build-metadata file. Ending Git status had the three
modified existing workplan files plus untracked 07 and
`tests/test_dominant_target_selection.py`; ordinary `git diff --stat` reported tracked files only
(90 insertions, 4 deletions). The test module and all changed documents were manually inspected.
Ruff/format/pytest remain **Unknown / requires verification** because the documented environment
blockers prevented tool startup.

## 2026-08-29 human-review test-scope correction and environment diagnosis

At `2026-08-29T13:27:29.8913566Z`, human review removed five integration concerns from the pure
selection contract: one artificial `tracking_runs_consumed` assertion and four rating/lifecycle/
event-projection assertions. The corrected test-facing internal API is limited to
`TargetSelectionStatus`, optional `TargetEligibilityResult`, `unique_track_evidence`,
`evaluate_dominant_target`, and `select_winning_track_segment`. File 07 records the deferred
orchestration, rating-engine, public-mapper, callback, and Apex compatibility acceptance plan.

Read-only diagnostics at `2026-08-29T13:28:37.2722277Z` produced these exact material results:
`Get-Command python` and `where.exe python` found no active Python; both `python -c` commands were
therefore unavailable. `.venv\\Scripts\\python.exe` exists (46,592 bytes), but both direct `sys`
and `typing` commands failed before Python started: `uv trampoline failed to spawn Python child
process`, `permission denied (os error 5)`. `.venv\\pyvenv.cfg` names CPython 3.12.13 and base home
`C:\\Users\\ENG-AMR MOHAMMED\\AppData\\Roaming\\uv\\python\\cpython-3.12-windows-x86_64-none`.
Read-only inspection of that base path was denied and then reported not found, so its executable and
standard-library `typing.py` cannot be verified. `uv 0.11.2` exists, but `uv python find` and both
`uv run python -c` commands fail because the UV cache path collides (`os error 183`).

Classification: **permissions prevent process creation** for the venv trampoline and **UV
cache/interpreter is broken** for UV commands. Base-interpreter availability and standard-library
path are **Unknown / requires further verification** because the referenced directory cannot be
read. No mutation, package installation, `typing` installation, virtual-environment recreation,
or repair was attempted. Because direct venv Python cannot import `sys`/`typing`, pytest collection
and execution were not attempted in this correction.

Final static verification at `2026-08-29T13:29:25.2850121Z` manually inspected the complete
corrected test module and file 07, source-counted exactly 28 `test_` functions, and found no
selection-unit reference to `gate_player_ratings`, `tracking_runs_consumed`, rating/Overall,
callback, event, or lifecycle assertions. `git diff --check`, trailing-whitespace, Sprint 1 link,
and forbidden-scope checks passed. Ending status contains only the pre-existing three modified
workplan documents plus untracked 07 and the new test module; no runtime, schema, configuration,
dependency, or lockfile file changed. Pytest collection/run remain blocked by the diagnosed
interpreter failure.

## 2026-08-29 local environment-repair baseline

At `2026-08-29T13:37:31.3179799Z`, the authorized recoverable repair began with a clean
runtime/config/dependency scope: only the expected Sprint 1 documentation and untracked test changes
were present, and `git diff --check` was clean. Immutable-input SHA-256 hashes are
`pyproject.toml` `69C5D92EAE08A3ED0A39207366B229535DA3C8C0CE20011AA6F86F5B8616A80C` and `uv.lock`
`5A09F68FCE163BA07487FEDC99D826637F31C2B2DE11D8D50583889CCFFFCF23`. Repository configuration
requires Python `>=3.12`.

Read-only launcher inventory: no `python` command exists; `py.exe` exists but `py -0p` reports no
installed Pythons; UV 0.11.2 exists. Its global cache path fails with `os error 183`, while the old
`.venv` is a UV trampoline configured for CPython 3.12.13. These findings authorize only the
explicitly requested isolated-cache/new-venv repair; no mutation has occurred in this entry.

At `2026-08-29T13:38:02.1988741Z`, the non-existent scoped temporary cache
`%TEMP%\\super7-uv-cache-20260829-02` was created after collision checks; `.venv` exists and
`.venv.broken-20260829-01` remains free. This is the only environment mutation so far and is
recoverable by removing that exact temporary directory after the task. UV still could not enumerate
managed Pythons because its global Python-install directory is access denied (`os error 5`), despite
using the isolated cache. No `.venv` move or project-file change occurred.

UV help confirmed `python install --install-dir`; at `2026-08-29T13:38:54.6645086Z`, UV downloaded
the 20.8 MiB CPython 3.12.13 distribution into the scoped temporary cache with `--no-bin` and
`--no-registry`. The direct executable starts, reports CPython 3.12.13, and imports its own
standard-library `Lib\\typing.py`. This is the selected compatible interpreter; no global UV path,
registry entry, project file, or dependency definition was changed.

At `2026-08-29T13:40:33.0585097Z`, the broken `.venv` was non-destructively renamed to
`.venv.broken-20260829-01` after confirming the destination was free. The old environment remains
available; before a replacement exists, rollback is `Move-Item .venv.broken-20260829-01 .venv`.

The first frozen sync created the replacement `.venv`, used the verified CPython, built the local
project, and downloaded lock-resolved artifacts, but did not finish within the execution window.
At `2026-08-29T13:41:40.8692379Z`, replacement Python successfully reported CPython 3.12.13 and
imported standard-library `typing`; `python -m pip` and `import pytest` reported unavailable. The
new environment is therefore partial, while the backup remains preserved. The only permitted next
repair action is one continuation of the identical frozen sync with the same lockfile/cache.

At `2026-08-29T13:42:56.0969012Z`, the identical frozen-sync continuation made `pytest 8.4.2`
importable in the replacement `.venv`. `python -m pip` remains unavailable, which is normal for
this UV-created environment and does not block direct `python -m pytest` use. No alternative
package command, dependency selection, lockfile update, or global-cache action was used.

At `2026-08-29T13:43:25.0081748Z`, both immutable-input hashes exactly matched their recorded
baseline, direct syntax compilation passed, and pytest collected exactly 28 new contract tests in
1.88 seconds. No local environment artifact appeared in Git status. The relevant existing offline
baseline then passed: **42 passed** in 2.43 seconds. Its sole warning was pytest cache write denial
under `.pytest_cache`; test behavior was unaffected.

The new focused contract suite collected all 28 tests and produced **28 expected failures**: every
one is `ModuleNotFoundError: No module named 'services.dominant_target_selection'`; there were no
skips, fixture/configuration, syntax, or environment failures. The combined focused set reports
**42 passed, 28 expected missing-module failures**. Ruff check passed. Ruff format check reports
that `tests/test_dominant_target_selection.py` would be reformatted; this environment-repair task
does not alter the user-owned test file, so formatting is deferred to a test-correction task.

Final environment-repair verification at `2026-08-29T13:44:40.7726880Z` reconfirmed the exact
baseline SHA-256 hashes for `pyproject.toml` and `uv.lock`; passed `git diff --check`,
trailing-whitespace, and Sprint 1 link checks; and found no changed runtime, schema, configuration,
dependency, or lockfile path. Both `.venv` and `.venv.broken-20260829-01` exist and are ignored by
Git. Ordinary diff stat contains only the pre-existing tracked Sprint 1 documentation edits; the
untracked 07 document and test file are intentionally excluded. The old environment is preserved;
do not roll it back while the replacement occupies `.venv`.

## 2026-08-30 isolated dominant-target implementation

At `2026-08-30T15:22:02.0749458Z`, the approved test contract was formatted with Ruff and remained
exactly 28 tests. Its red result was 28 failures, each solely
`ModuleNotFoundError: services.dominant_target_selection`. After inspecting `PlayerTrack`,
`TrackSegment`, existing segment ranking, and settings, the isolated
`src/services/dominant_target_selection.py` module was added. It derives evidence from unique frame
keys, rejects invalid metadata/non-finite confidence conservatively, applies current qualification
settings and inclusive margin semantics, breaks equal evidence deterministically by `track_id`, and
uses `rank_segments` only after an established winner is selected. It does not import or alter
routes, composition, ratings, schemas, callbacks, formulas, or settings.

At `2026-08-30T15:23:31.7605994Z`, the new contract suite passed **28/28** in 0.33 seconds after
the new module was formatted. Ruff check passed; Ruff format check reports both changed Python files
formatted; direct `py_compile` and import checks passed. At `2026-08-30T15:23:47.7627283Z`, the
existing six-module offline baseline passed **42/42** in 2.31 seconds and the combined focused set
passed **70/70** in 1.86 seconds. Each pytest command emitted only the pre-existing pytest-cache
write-permission warning. No inference, live request, pipeline integration, or public contract
change occurred.

Final static verification at `2026-08-30T15:26:11.9946232Z` passed `git diff --check` and the
targeted trailing-whitespace scan. The intended scope is the formatted approved contract test, the
new pure service module, and Sprint 1 documentation/status records; no route, composition, API,
schema, rating, formula, settings, dependency, lockfile, CV, live, deployment, staging, commit, or
push change exists. A generic link-check attempt was rejected because it incorrectly resolved
document-relative paths from the repository root; it made no repository change and is not a broken
link finding. `pyproject.toml` and `uv.lock` retain their baseline SHA-256 hashes. The isolated
module is ready for human review only; production pipeline integration remains deferred.

## 2026-08-30 integration-tests-only feasibility stop

At `2026-08-30T15:51:03.2508224Z`, the mandatory tests-only integration preparation completed
without changing runtime code. The branch and HEAD were confirmed as `the-new-inhancement` and
`275141d7ed57215352c47a3e5d5c14a7d20fd89d`; the starting worktree contained only the reviewed
Sprint 1 documentation, pure selector module, and its unit test. The inspected actual boundary was
`api.routes._analyze_uploaded`: it runs the tracker once, then remains segment-first and never
consumes `TargetEligibilityResult` or invokes `evaluate_dominant_target`. Its completed projection
requires `SelectedPlayer`, while `NonCompletedResponse` has no player-rating projection.

No focused integration test can truthfully specify all required outcomes at that boundary without
first adding a prohibited production seam: there is no internal carrier for target establishment,
selected winning segment, target-gated rating availability, or the proposed Technical-plus-one-core
Overall eligibility. Existing `PlayerRatingEngine` behavior also intentionally permits Overall from
Physical Activity plus Ball Involvement, so asserting the proposed target policy would change a
formula/availability contract rather than test integration. Public mapper and callback assertions
would require the deferred Apex schema/compatibility decision. Per the task stop condition, no new
test file was added and no collection, red test, baseline, combined, Ruff, or format command was
run. The missing seam and no-go are documented for human review; runtime remains unchanged.

## 2026-08-30 resolver-approved integration-tests-only stop

At `2026-08-30T16:03:19.1944325Z`, human review approved one future internal
`resolve_dominant_target(...)` helper and an immutable eligibility-plus-selected-segment result. This
is sufficient to avoid a manager, DI framework, or second tracking call in future route wiring.
Mandatory source inspection nevertheless reconfirmed a remaining stop condition. The actual
`_analyze_uploaded` return union is `CompletedResponse | AmbiguousResponse | NonCompletedResponse`.
`CompletedResponse` requires `selected_player`; `NonCompletedResponse` explicitly has no player
ratings, Overall, or Overall-confidence fields; and the public mapper maps noncompleted results to
a failure-shaped public projection. Therefore no existing nullable response fields can represent the
approved successful target-unavailable analysis with all player-attributed ratings, Overall, and
Overall confidence null.

The requested integration suite cannot be created faithfully without deciding a result-projection
representation and changing a prohibited schema/mapper contract. No test file was added, so
collection, red tests, existing green suites, Ruff, formatting, syntax, or test commands were not
run. This is not an environment or fixture failure. Runtime, API, schema, formula, settings,
dependency, model, infrastructure, deployment, commit, and push state remain unchanged.
