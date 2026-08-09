# Phase 0: Safety Hardening

## Goal

Stop normal analyses from retaining debug media indefinitely and stop local filesystem paths from appearing in responses, without changing video-analysis algorithms or V2 behavior.

## Architecture and changes

`core.config.DebugSettings` is explicit and defaults to disabled. Video and frame capture are independent opt-in flags; `save_on_failure` is reserved policy configuration and defaults off. `main.create_app` passes the configured retention count to the existing request-scoped `ArtifactManager`.

`api.routes._analyze_uploaded` now copies the source and invokes `render_debug_video` only when debug is enabled and at least one output is requested. The renderer only creates requested MP4 and/or frame directory. Artifact paths remain internal to the request-owned session; `routes._public_debug_artifact_references` exposes filename-only references in V1 diagnostics.

## Before / after

Before, every admitted analysis copied the source, retained the request directory indefinitely, rendered an MP4, wrote every frame JPEG, and returned local paths. After, default requests produce no debug media, empty sessions are cleaned automatically, opted-in media uses existing isolated directories and bounded retention, and responses contain no local paths.

## Migration notes and risks

Set `DEBUG_ARTIFACTS_ENABLED=true` and one or both media flags to preserve developer capture. Set `DEBUG_RETAINED_SESSIONS` above zero only when short-term internal access is needed. Existing clients that consumed absolute `debug_artifacts` paths must switch to server-side artifact access; absolute path exposure is intentionally removed. `save_on_failure` is policy configuration only in Phase 0; failure-specific capture remains Phase 1 work.

## Tests and lessons learned

Phase 0 tests cover disabled/enabled settings, failed-session cleanup, bounded retention, and public path serialization. Existing artifact lifecycle tests remain the authority for staging/quota/isolation. The key lesson is that debug output is an operational policy, not a normal analysis side effect; request isolation alone does not make unbounded retention safe.
