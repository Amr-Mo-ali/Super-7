# Phase 2: Configuration Ownership & Contract Consolidation

## Objective

Make configuration ownership explicit without changing analysis, scoring, arbitration, or public API behavior.

## Previous and target state

Previously operational values were split between `core.config.Settings`, profile dictionaries, rating constants, arbitration constants, and a route-level admission literal. The target is one owner per value with a compatibility facade: `Settings` remains the runtime contract; `config/` owns infrastructure, debug/retention, scoring, and arbitration constants.

## Ownership rules and map

| Owner | Consumers | Default / override | Public impact | Test |
|---|---|---|---|---|
| `core.config.Settings` + `football_profiles` | video, tracking, movement, interaction, events | code defaults; selected environment overrides | analysis evidence/quality | existing service tests |
| `config.analysis` | `main.create_app` | one active analysis | capacity behavior | configuration ownership test |
| `config.debug` / `config.retention` | `Settings.from_environment`, artifact setup | disabled / `DEBUG_*` variables | debug-only | configuration + Phase 0 tests |
| `config.scoring` | game intelligence | existing product weights/gates | provisional rating | GI tests |
| `config.arbitration` | EventArbitrator | existing V0.1 thresholds | V2 event representation | arbitration tests |

`services.player_rating.config` and `services.event_arbitration.config` retain re-exports so existing imports remain valid. Tracking, movement, interaction, and technical-event thresholds remain in `Settings`; their `config/` modules document that compatibility ownership rather than duplicate values.

## Migration notes, tests, risks, lessons

No environment variable, default, threshold, scoring weight, or public contract changed. `DebugSettings` gained negative-retention validation; valid existing settings are unaffected. Tests cover defaults, environment override, profile loading, validation, stable arbitration values, and compatibility re-export identity. The main remaining risk is that the large compatibility `Settings` facade still groups many analysis domains; splitting it further would require broader consumer migration and is intentionally deferred. The lesson is to centralize a value before exposing a new override, and retain compatibility re-exports only during a deliberate migration.
