> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Discovery log

All timestamps are UTC. Commands are reproduced in sanitized form; no URL, token, IP address,
personal player data, or raw diagnostic evidence is included. Only Sprint 1 documentation files
were intentionally created or modified; no runtime files were changed.

Time-recording correction: original discovery entries record known dates but did not capture exact
UTC times; those historical entries are preserved rather than reconstructed. This correction pass
uses full UTC timestamps for new entries.

| # | Time | Action / purpose | Evidence inspected / command | Result | Classification | Files changed | Question / next action |
|---:|---|---|---|---|---|---|---|
| 1 | 2026-08-27 | Read task and mandatory onboarding | pasted brief; `AGENTS.md`; all five `docs/handoff/*.md` | Discovery-only scope and evidence guardrails confirmed. | Documented decision | none | Inspect contracts, ADRs, runbooks and source. |
| 2 | 2026-08-27 | Establish repository baseline | `Get-ChildItem -Force`; `git status --short`; `git branch --show-current`; `git rev-parse HEAD`; `git remote -v`; `rg --files …` | Clean start; branch and commit recorded in README. `docs/vision` absent. Remote was listed without credentials. | Implemented and production-wired | none | Trace public API, queue and child boundary. |
| 3 | 2026-08-27 | Read governing decisions and operations | `Get-Content -Raw docs/decisions/*.md docs/contracts/*.md docs/runbooks/*.md .github/workflows/*.yml` | ADRs distinguish current code from future durability; CI runs no production inference; deploy is main-only after CI. | Documented decision | none | Compare with current code/tests. |
| 4 | 2026-08-27 | Required symbol discovery | `rg -n -S "playerId|player_id|track_id|target|selector|overall|…|callback" src tests docs` | Required concepts occur in API, pipeline, selectors, rating engines, mapper and focused tests. Output was truncated, so focused symbol/file reads followed. | Implemented and production-wired | none | Inspect current composition, selectors and formulas. |
| 5 | 2026-08-27 | Trace implementation ownership | Focused `rg` plus source reads for schemas, routes, queue, child, tracker, selection, segment selection, rating engines, mapper, callback, configuration and focused tests | `playerId` stays request/callback identity; selectors use visual-track evidence only; ratings are provisional and gated, but target eligibility is not a rating gate. | Implemented and production-wired | none | Draft discovery documents and run safe focused tests. |
| 6 | 2026-08-27 | Create required discovery pack | `apply_patch` | Added only `docs/workplans/sprint-1/*`; no runtime files changed. | Proposed | this directory | Verify links, diff, and focused tests. |
| 7 | 2026-08-27 | Run focused deterministic tests | `uv run pytest -q <selector/rating/contract/processor tests>`; direct `.venv` pytest fallback | Neither launcher reached pytest: local cache/interpreter child creation failed with permission errors. No test result is claimed. | Unknown / requires verification | none | Re-run in an environment where the project Python launcher may create child processes. |
| 8 | 2026-08-27 | Verify documentation diff and links | `git diff --check`; PowerShell repository-relative Markdown-link check; `git status --short` | No `git diff --check` errors; link check resolved every repository-relative discovery-doc link. Git reports only untracked `docs/workplans/`. `git diff --check` does not inspect untracked files. | Implemented but not production-wired | none | Human/review-tool Markdown rendering still advisable. |
| 9 | 2026-08-27 | Documentation-correction provenance check and correction | `git rev-parse HEAD`; `git diff --name-status 7920375… f2d1e834…`; `git log -1 f2d1e834…`; continuation brief | Current HEAD is documentation commit `f2d1e834…`; range contains only the eight Sprint 1 Markdown files. Per continuation brief, it was committed and pushed after the original discovery work. Local evidence verifies the commit/range, not remote push receipt. | Implemented but not production-wired | Sprint 1 Markdown only | Correct provenance, traceability, rating audit and decision register. |
| 10 | 2026-08-27 | Verify correction documentation | `git diff --check`; PowerShell repository-relative Markdown-link check; targeted terminology search; `git status --short` | No whitespace errors; all repository-relative Sprint 1 links resolve. Git reports only the eight modified Sprint 1 Markdown files. Git emitted LF-to-CRLF warnings for these working-copy files; no content error was reported. | Implemented but not production-wired | Sprint 1 Markdown only | Correction complete; approval decisions remain blocked. |
| 11 | 2026-08-27 | Validate detailed-rating and Overall symbol references | `Get-Content -Raw src/services/detailed_rating/engine.py src/services/player_rating/engine.py` | Confirmed `DetailedRatingEngine._event_score`, `_ball_control`, `_visible_movement_activity`, and `PlayerRatingEngine._overall` references used by the expanded rating audit. | Implemented and production-wired | none | No runtime change; documentation references are verified. |
| 12 | 2026-08-27T19:24:32.0386968Z | Capture correction-pass Git evidence and initial status | `git status --short`; `git show --stat --summary f2d1e834…`; baseline-to-documentation `git diff --stat` and `--name-status` | Initial correction tree has exactly eight modified Sprint 1 Markdown files. Commit/range show eight documentation-only additions (325 insertions); no runtime behavior changed between baseline and documentation commit. | Implemented but not production-wired | none | Add hierarchy, future metrics and truthful final verification. |
| 13 | 2026-08-27T19:25:31.4048622Z | Inspect every modified discovery document and run correction checks | Read all eight Sprint 1 Markdown files; `git diff --check`; `rg` trailing-whitespace check; link checker; scope/status/stat check; search for Markdown tooling | All eight inspected. No `git diff --check` content error, no trailing whitespace, and all repository-relative links resolve. Only eight Sprint 1 Markdown files are modified. Git emitted LF-to-CRLF warnings. No repository Markdown-specific checker was found. No tests reattempted; original environment limitation remains. | Implemented but not production-wired | none | Final documentation summary only. |
| 14 | 2026-08-27T19:26:13.3951438Z | Final correction validation | `git diff --check`; trailing-whitespace scan; repository-relative link check; scope check; `git diff --stat`; `git status --short` | No diff-check content error, trailing whitespace, broken repository-relative links, or out-of-scope changes. Eight Sprint 1 Markdown files only; captured stat was 246 insertions and 76 deletions. LF-to-CRLF warnings remain informational. | Implemented but not production-wired | none | Human and Apex review remains the next permitted step. |

## Commands and checks

| Command (sanitized) | Purpose | Result / exit | Changed files? |
|---|---|---|---|
| `Get-Content -Raw <brief and handoff files>` | Read task and canonical handoff | completed / 0; output truncation handled by later reads | no |
| `git status --short; git branch --show-current; git rev-parse HEAD; git remote -v` | Baseline state | completed / 0; clean, branch/commit recorded | no |
| `rg --files …` | Inventory source, tests and governing documentation | completed / 1 because `docs/vision` is absent | no |
| `Get-Content -Raw <ADRs/contracts/runbooks/workflows>` | Read governing material | completed / 0 | no |
| `rg -n -S <required terms> src tests docs` | Required reference search | completed / 0; output truncated, not treated as exhaustive proof | no |
| focused `rg` / source reads | Definition and test trace | completed / 0 | no |
| `uv run pytest -q <focused tests>` | Safe behavioral verification | not executed: UV cache/interpreter permission error before pytest startup | no |
| `.venv\\Scripts\\python.exe -m pytest -q <focused tests>` | Fallback behavioral verification | not executed: UV trampoline permission error before pytest startup | no |
| `git diff --check` | Whitespace inspection | completed / 0; applies to tracked diff only, hence excludes newly untracked files | no |
| PowerShell Markdown-link check | Verify discovery-doc repository-relative links | completed / 0; all resolved | no |
| `git diff --name-status 7920375… f2d1e834…` | Verify baseline-to-documentation range | completed / 0; eight Sprint 1 Markdown additions only | no |
| `git diff --check`; link/terminology checks | Validate correction documentation | completed / 0; links resolve, only Sprint 1 Markdown modified; LF-to-CRLF warnings observed | no |
| `Get-Content -Raw <detailed/player rating engines>` | Validate audit symbol references | completed / 0 | no |
| `git show --stat --summary f2d1e834…`; baseline-range diff stat/name-status | Capture required provenance evidence | completed / 0; documentation-only additions | no |
| read-all-documents; diff/whitespace/link/scope/Markdown-tool checks | Final correction verification | completed / 0; all static checks pass, LF-to-CRLF warnings; no Markdown checker available | no |
| final diff/whitespace/link/scope/stat/status checks | Final correction validation | completed / 0; all checks pass; only Sprint 1 Markdown modified | no |

Assumptions avoided: `playerId == track_id`; event confidence equals skill; pixels equal metres or
fitness; documentation proves runtime behavior; track continuity proves initial identity; null is
zero; and current scores are calibrated. The original discovery session performed no inference,
live request, deployment, configuration mutation, package install, model download, commit or push.
Afterward, the documentation was committed as `f2d1e834…` and, per this correction request, pushed;
this correction does not rewrite that history.

## Contract approval documentation activity

The original discovery entries retain their historical timestamp limitations. The task brief read
occurred before a tool-provided UTC timestamp was captured and is recorded as such rather than
inventing a time. Subsequent entries use captured full UTC timestamps.

| UTC timestamp | Action | Purpose | Files/symbols inspected | Command | Result | Classification | Files changed | Decision affected | Unresolved question | Next action |
|---|---|---|---|---|---|---|---|---|---|---|
| not captured | Read contract-approval brief | Establish documentation-only scope and approved product facts. | User-provided brief | `Get-Content -Raw <attachment>` | Brief supplied dominant-candidate contract requirements. | Documented decision | none | D1–D15 | Apex API confirmation remains pending. | Complete onboarding/source verification. |
| 2026-08-29T12:09:36.5374540Z | Inspect selector and relevant rating flow | Verify contract feasibility and current selector limits. | `segment_selection.py:build_segments,rank_segments,select_segment`; `selection.py:WeightedTargetPlayerSelector`; route/rating mappers; focused tests | `Get-Content -Raw <source/tests>`; `git log`; baseline diff | Default segment mode sorts composite quality and selects first; no runner-up dominance/status/input-guarantee binding. | Implemented and production-wired | none | D1, D2, D3, D6, D12, D13 | Operational dominance rule/threshold unknown. | Create ADR and proposed behavioral contract. |
| 2026-08-29T12:09:36.5374540Z | Create proposed contract artifacts | Formalize approved product semantics without runtime change. | Discovery pack and prior ADRs | `apply_patch` | Added `ADR-005-dominant-visual-target-mvp.md` and `target-selection-contract-v1.md`. | Proposed | ADR and contract | D1–D14 | Apex public/API confirmation pending. | Align discovery documents. |
| 2026-08-29T12:09:36.5374540Z | Align discovery documentation | Correct pipeline and record semantic decisions. | Sprint 1 README, target/identity, ratings, decision register, implementation map | `apply_patch` | Added documented-decision sections, approved semantic addendum, and automatic-flow order. | Documented decision | Sprint 1 Markdown | D1–D15 | Thresholds and public shape unresolved. | Static verification and final record. |
| 2026-08-29T12:12:33.7075214Z | Run contract-documentation static checks | Verify scope, links, whitespace, Git state and Markdown tooling. | New ADR/contract and modified workplan files | `git status`; `git diff --stat`; `git diff --check`; `rg`; PowerShell link checker | Branch/HEAD `the-new-inhancement`/`f8664cd…`; only intended Markdown files changed; diff check clean; no trailing whitespace; links resolve; no Markdown tooling found. | Implemented but not production-wired | none | D1–D15 | Tests remain unexecuted; thresholds/API pending. | Update verification record. |
| 2026-08-29T12:14:03.4634315Z | Recheck corrected hierarchy and final static state | Confirm automatic ordering, conceptual-language safety, whitespace, links and diff scope. | ADR, contract, all changed Sprint 1 documents | `rg`; `git status`; `git diff --check`; whitespace/link check; `git diff --stat` | No stale automatic target-before-tracking order found; required caveats remain present; no diff-check/trailing-whitespace/link error. Seven modified Sprint 1 docs plus two new Markdown docs are the only changes. | Implemented but not production-wired | none | D1–D15 | Apex/API confirmation and thresholds remain unresolved. | Final response. |
