# ADR-003: Null and evidence policy

## Status definitions

- **supported:** an implemented score with a narrow, documented calculation and direct code evidence; this is not a calibration claim.
- **provisional:** an implemented video proxy whose football interpretation has material limitations or lacks validation.
- **insufficient_evidence:** analysis completed far enough to evaluate a gate, but the required evidence was absent, too limited, unreliable, non-finite, or ambiguous.
- **unsupported:** the current pipeline has no defensible calculation/evidence primitive for the field.
- **failed analysis:** analysis did not complete; it is a response lifecycle state, not a zero score or an insufficient-evidence score.

## Rules

`null` means no numeric rating is defensible for the field in the reported status/reason. It is never converted to zero for aggregation, display, or convenience. A field does not become numeric merely because the response contract contains it.

Confidence expresses bounded confidence in the available video/event evidence and calculation inputs. It is separate from the score and is not player-skill quality, validation, calibration, or a probability of football success. Low confidence must remain visible; a high score with low confidence is possible under current formulas.

Evidence gates must be evaluated before exposing a number. A gate may reject for missing analysis, duration, observation count, coverage/quality, target attribution, event arbitration, finite values, component availability, or any field-specific requirement. `unsupported` fields remain null rather than borrowing unrelated proxies.

Before replacing any null with a number, an implementation must provide all of the following:

1. A defined football meaning that does not overclaim.
2. Observable, field-relevant evidence primitives.
3. A documented, versioned calculation and limitations.
4. Minimum evidence gates and explicit null/reason behavior.
5. Separate score and confidence semantics.
6. Tests for normal, insufficient, unsupported, ambiguous, and non-finite cases.
7. A validation/calibration plan appropriate to the product claim.
8. No misleading product claim in code, API, callback, or documentation.

Material changes to these semantics require a decision record and an explicit contract-compatibility review.
