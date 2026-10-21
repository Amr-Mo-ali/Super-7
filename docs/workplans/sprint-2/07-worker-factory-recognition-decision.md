# Worker factory-recognition policy decision record

> Decision date: 2026-10-12
> Status: Owner-approved policy; documentation awaiting independent fidelity review
> Authority: Normative rules are in [Planning-H §7](05-worker-repository-construction-contract.md#7-approved-constructor-time-factory-recognition)
> Authorization: Documentation only; no implementation, test edits or commit approval
> Runtime activation: Not approved

This is a non-normative decision record. It records the owner's explicit disposition of the
designated FR1–FR11 v2 proposal, rationale and evidence limitations. It does not create additional
contract options or requirements. [The row contract](06-worker-repository-row-contract.md) receives
only a construction-boundary cross-reference; C1–C10 and existing factory/transaction rules are unchanged.

## Authoritative source and approval

| Source evidence | Verified value |
| --- | --- |
| Sole transcription input | `factory-recognition-FR1-FR11-canonical-proposal-v2.md` |
| Bytes | 72,016 |
| Logical lines | 891 |
| LF characters | 890 |
| SHA-256 | `e09356d4a5cb65821fbac503171fd91ef770346b00f035822068966eed4f0da8` |
| Encoding | UTF-8, no BOM, LF-only; source has no final newline |
| Approval | Explicit owner decisions dated 2026-10-12 |

The source remains unchanged. Its proposal-status wording records its historical review state;
the owner's subsequent explicit approval supplies the adopted disposition. The original proposal
whose SHA-256 begins `0923372c` is superseded and was not used as the transcription source.

Historical correction-task instructions supplied hook-safety provenance, not durable Planning-H
authority. The owner now explicitly approves the durable safety wording and native mechanism.
Planning-H's UUID-result subclass rejection was not authority for descriptor/partial-subclass policy;
the latter decisions receive their own explicit approval below.

## Approved decisions and rationale

| ID | Owner disposition | Rationale / boundary |
| --- | --- | --- |
| FR1 | Existing construction obligations retained | Preserve exports/error identity, constructor, private ports, five operations, validation order and fixed errors. Acceptance is not proof of arity, execution or result validity. |
| FR2 | Approved as written in v2 | Exact-native one-level descriptor recognition preserves native controls without traversing nested/opaque stored targets. |
| FR3 | Approved as written in v2 | Descriptor-subclass stored-target opacity avoids equating stored targets with actual custom binding or direct calling. |
| FR4 | Durable wording and native mechanism approved | Prevent hook execution during acquisition and processing; catching hook exceptions is insufficient. Demonstrated metaclass, key-equality and weak-reference/cache-hashing defects require later repair. |
| FR5 | Approved as written in v2 | Traverse exact partials, keep subclasses opaque for stored-target extraction, and preserve independent recognition of the original factory's native asynchronous call override. |
| FR6 | Approved as written in v2 | Separate binding from direct invocation and restrict roots; an opaque inner object cannot reopen traversal through fallback. |
| FR7 | Approved | Identity repetition of an exact partial ends that path as opaque; no arbitrary depth limit, new error or invocation deadline. |
| FR8 | Existing invocation obligations retained | Preserve placement/counts, UUID object identity, invalid-return rejection, exception identity and restricted cleanup; no internal retry. |
| FR9 | Non-normative qualification retained | Project range and measured interpreter coverage differ; compatibility maintenance remains necessary. |
| FR10 | Approved | Conservative exact-str evidence avoids hook-dependent lookup claims; opacity dominates exact entries and stops inheritance, but native flags remain independent. |
| FR11 | Import-time bootstrap approved | Controls derive the profile through the public API without inspecting a supplied factory; normal import synchronization avoids constructor initialization locking. |

Option A is adopted. The owner approves one later correction reviewed in two logical parts:

1. FR4/FR10 lookup safety and FR11 bootstrap.
2. FR2/FR3/FR5/FR6/FR7 wrapper and repetition policy.

This is the intended implementation shape, not implementation authorization. Demonstrated hook
repairs and all other outstanding implementation/verification obligations remain blockers before
GREEN-H commit approval.

## Considered, not-adopted alternatives

These alternatives are historical design comparisons, not available contract options:

| Alternative | Considered tradeoff | Disposition |
| --- | --- | --- |
| Option B: permit adversarial dictionary-key comparison hooks | Smaller repair could retain more stdlib/hash lookup, but recording/raising key hooks remain; attribute, descriptor, metaclass and cache-hashing repairs would still be needed. | Not adopted; approved hook safety is not relaxed. |
| Construction-time bootstrap | Delays compatibility failure, but requires private cache/lock management, waiting-constructor handling and bootstrap failure precedence between callability and known-async recognition. | Not adopted; bootstrap is import-time. |
| Target-text-only opacity | Fewer unrelated keys suppress recognition, but unrelated custom keys can intercept lookup. | Not selected; conservative FR10 is adopted. |
| Native textual matching of subclass keys | Hook-free text comparison does not establish the interpreter's selected target. | Not selected. |
| No descriptor unwrapping | Simpler, but discards retained native async-descriptor controls. | Not selected; FR2 is adopted. |
| Safely proven native subclass behavior | Would require proof of relevant binding/call behavior rather than mere subclass membership. | Not selected; approved descriptor/partial-subclass opacity applies. |
| Split correction | An intermediate safety stage would need safe retained subclass proof and no opaque fallback, while known binding/cycle defects could remain. | Not selected; one correction, two logical review parts. |
| Move profile publication to the last module statement | A possible separate owner alternative after the reload finding. | Not adopted; v2's bootstrap → publication → remaining initialization order is preserved. |

## Evidence provenance and limits

The v2 proposal and preceding independent reviews distinguish three evidence classes:

| Class | Recorded evidence | Limit |
| --- | --- | --- |
| MEASURED | Current-adapter constructor and separate Python invocation observations, including metadata/shadow, descriptor/partial, metaclass/key and indirect hashing probes. | Not execution of the replacement recognizer. |
| MEASURED | Fresh native marker/flag/control feasibility observations on CPython 3.12.13, normally and under `-O`, with no probe-body execution. | Native primitive controls are not production-bootstrap verification. |
| MEASURED | Bounded current-recognizer cycles stopped by an external observation bound; a 1,200-node acyclic chain completed. | No cyclic factory invocation or replacement termination proof. |
| MEASURED | Generic external loader module showed first-import removal/retry, pre-publication old-value identity, post-publication new-value retention, cached imports, recovery and normal context, normally and under `-O`. | Generic-loader probes are not production-bootstrap verification. |
| SOURCE | Installed inspect/importlib, repository contracts, current recognizer and import relationships. | Source reasoning does not establish replacement execution or live PostgreSQL behavior. |
| PREDICTED / required | Approved replacement recognition, no custom hooks and FR11 production lifecycle. | Remain unimplemented and require later authorized verification. |

The hashing controls reported one current hook for recording and raising meta-metaclass variants,
zero pool/factory interactions, and zero hooks in native MRO/namespace primitive controls. They do
not establish an executed hook-free replacement.

The bounded v2 review independently found only three line replacements at source lines 449, 454
and 765, in two hunks. Reversing them reproduced the original bytes. These qualify reload failure
before versus after publication and the pre-publication regression oracle; no other policy changed.

No post-publication production regression was added. That case remains a documented, untested
production qualification. Failed reload can leave old or newly published state; neither branch
establishes safe module use or rollback. Publication was not moved and no repair/revocation mechanism
was adopted.

Executed evidence is CPython 3.12.13 only. The project declares Python `>=3.12`, with Ruff/Mypy
targeting 3.12. Classmethod descriptor chaining was removed in Python 3.13; no execution coverage of
that or another interpreter is claimed. [Python classmethod documentation](https://docs.python.org/3.13/library/functions.html#classmethod)
The narrowed rule removes a nested-descriptor recognition branch, not every interpreter difference.
The public marker API still has an observed metadata representation that requires maintenance.
[Python marker API](https://docs.python.org/3.12/library/inspect.html#inspect.markcoroutinefunction)

Unmodified trusted primitives/stdlib are assumed. Global monkeypatching, audit hooks, concurrent
mutation and concurrent reload remain outside the guarantee. Stable-graph construction termination
does not imply invocation termination or protection against unrestricted resource exhaustion.
These limitations also remain in normative Planning-H §7, not only in this record.

Current static import discovery found a docstring-only `src/adapters/__init__.py`, a
`TYPE_CHECKING` composition import and a lazy runtime import in the worker-repository contract test,
and no runtime composition-root import. Direct consumers, future eager imports and import-executing
tooling can encounter profile incompatibility; ordinary static analysis does not execute the adapter.
Static discovery cannot rule out externally constructed dynamic imports and does not imply activation.

## Planning estimates, operational costs and follow-up candidates

The v2 estimate is 100–150 nonblank production lines for the complete recognition facility.
The preceding review's approximate 75–125 net additional facility lines is a planning estimate,
not a contract requirement or measured implementation diff. No prototype measured either figure.
Native access/entry observation, raw-call lookup, function classification, traversal/repetition and
marker bootstrap account for the facility; the current recognizer/constructor recognition block
would be replaced rather than supplemented with a general framework.

Costs include overlap with inspect, dependence on public-API metadata representation, native entry
scans instead of constant-time lookup, strongly retained traversal references and import/collection
compatibility failures. No total-construction bound or universal compatibility monotonicity is claimed.

Opaque invalid factories can incur acquisition, locking, invocation and rollback on each caller
retry. Operational mitigation is a separately scoped future candidate; none is adopted now.
There is no internal retry loop, logging, metric, alert, runtime wiring or new public API.

Future production review must verify approved native acquisition/processing and bootstrap behavior,
not merely passing tuple fixtures or primitive controls. Applicable source and live database evidence
remain separate, including locking, uniqueness arbitration, mutation clocks, recovery/reclaim fencing
and writer-blocking downgrade protection. Runtime activation remains separately gated.

## Fidelity and amendment inventory

| Documented location | Source / authorizing decision | Before → after relationship |
| --- | --- | --- |
| 05 §2 known-asynchronous row | v2 §2 FR1–FR7/FR10 | Broad wording → approved recognition rules; fixed synchronous-factory error unchanged. |
| 05 §7 FR1 and FR8 references | v2 §2 FR1/FR8; prior Planning-H §§1–3/6 and row C4 | Existing declarations, validation/counts, UUID and cleanup obligations retained by reference; no unnecessary declaration duplication. |
| 05 §7 FR2/FR3/FR5/FR6 | v2 §2 matching FR decisions | New approved recognition/binding/opacity boundary. |
| 05 §7 FR4 and native mechanism | v2 §2 FR4 and §4; owner Option A | Task-level provenance → durable approved hook safety and native mechanism. |
| 05 §7 FR7 | v2 §2 FR7 | Approved identity-repetition disposition and unchanged stated limitations. |
| 05 §7 FR9 note | v2 §2 FR9 | Non-normative qualification retained; not a support-range restriction. |
| 05 §7 FR10 | v2 §2 FR10 | Approved key evidence, opacity and flag precedence. |
| 05 §7 FR11 | v2 §2 FR11 | Import-time choice adopted; controls, exact failure, propagation/context, private cache/publication and corrected lifecycle preserved. |
| 05 §7 import effects/limitations | v2 §2 FR4/FR7/FR11 | Explicit pure-initialization permission; no runtime wiring/external effects or expanded safety guarantee. |
| 05 §7 defect inventory | v2 §5; FR4 | Current defects → required repairs, not claims of completed repair. |
| 05 §7 acceptance outcomes | v2 §6; FR1–FR7/FR10 | Proposed-outcome column → approved requirements; measured current/invocation columns are not promoted to replacement execution evidence. |
| 05 §7 regression obligations | v2 §8; FR1–FR11 | Future authorized verification requirements retained; failed-reload oracle limited to pre-publication failure and old tuple identity. |
| 06 construction paragraph | v2 §10; FR4/FR11 clarification | One cross-reference only; existing factory clauses and all row/lifecycle rules unchanged. |
| 07 record | v2 §§1, 3, 5–7, 9–10; FR9 and owner decisions | Non-normative rationale, alternatives, evidence, estimates, costs and sequencing; no new policy. |

## Owner-decision register and execution gates

| Choice | Disposition | Remaining gate |
| --- | --- | --- |
| Recognition FR2/FR3/FR5/FR6 | Approved as written in v2 | Separate production/test correction and independent review. |
| Hook safety/native mechanism; Option A | Approved; Option B not adopted | Demonstrated violations repaired and verified before GREEN-H commit approval. |
| FR7 repetition → opaque | Approved | Stable-graph regressions; no invocation termination claim. |
| FR10 conservative opacity | Approved | Acceptance delta and zero-hook requirements verified in the later implementation. |
| FR11 import-time bootstrap | Approved; construction-time alternative not adopted | Production controls, failure/caching/reload regressions and compatible import evidence. |
| One correction / two logical parts | Approved intended sequence | Implementation authorization has not been granted by this task. |
| Documentation | Authorized; fidelity review required | No staging or commit authorization. |
| Database/live safety and activation | Not authorized | Applicable live evidence and explicit runtime authorization remain separate. |

No code suites or production repair are established by this documentation. Public exports, error
aliases, constructor parameters, operation signatures, SQL, lifecycle, snapshots, Fingerprint V1,
HMAC framing and callbacks remain unchanged. README, Planning-F and migrations are protected.
