> Status: Discovery evidence — not an approved implementation contract.
>
> This document describes the repository state inspected at the recorded Git
> commit. Proposed Sprint 1 behavior remains subject to review and approval.

# Decisions required before Sprint 1 implementation

| ID | Decision needed / current behavior | Recommended minimum (Proposed) | Compatibility / owner | Blocker? |
|---|---|---|---|---|
| D1 | Define initial target establishment. Current selector establishes only analyzability. | Only an explicitly agreed, conservative evidence condition may be `ESTABLISHED`; otherwise `NOT_ESTABLISHED`. | Product + Apex coordination | yes |
| D2 | Can legacy automatic selection establish target? Current code does not prove requested identity. | No; treat legacy automatic selection as `NOT_ESTABLISHED` until a documented restricted eligibility contract is approved. | Product + Apex | yes |
| D3 | Ambiguity representation. Segment mode has no margin gate. | A status/reason, never silently choose a rated target. | Super-7 + Apex | yes |
| D4 | Public target and future continuity fields do not exist. | Add/version only after Apex agrees shape; continuity initially `NOT_EVALUATED` if exposed. | Apex coordination | yes |
| D5 | Rating availability differs between V2 status/reason and callback detailed nulls. | Preserve null; add explicit availability/reason only in an agreed versioned surface. | Apex coordination | yes |
| D6 | Overall is available with two categories and no target gate. | If target is not established, all player ratings and Overall unavailable; define its confidence as null. | Product + Apex | yes |
| D7 | Minimum rating coverage and confidence rule. | Do not change category formula incidentally; decide whether existing two-category rule remains after eligibility. | Product decision | yes |
| D8 | Game Intelligence and Physical labels overclaim. | Preserve values only with explicit provisional wording or make unavailable by agreed contract; do not reinterpret silently. | Product + Apex | yes |
| D9 | Pass/shot numeric fields expose event confidence under skill labels. | Change wording/shape only through compatible versioning or make unavailable. | Product + Apex | yes |
| D10 | API versioning. | Prefer additive versioned fields/reason codes; do not break existing callback consumers without Apex migration. | Apex coordination | yes |

Unresolved technical evidence: no visual link mechanism exists, and no labelled data validates a
restricted automatic-establishment rule. The owner must explicitly decide whether a new API version
is needed and which legacy fields may change semantic meaning without a shape change.
