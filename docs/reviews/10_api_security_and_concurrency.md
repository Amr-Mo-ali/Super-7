# Phase 10 — API Security and Concurrency Review

**Scope:** FastAPI routes, upload endpoint, response construction, exception
mapping, temporary storage, HTTP status codes, request limits, concurrency/timeouts,
and public diagnostics.

**Review constraint:** no background queue was introduced, no production endpoint
behavior was changed, and no production video inference was run.

## Files reviewed

- `src/main.py`
- `src/api/routes.py`
- `src/services/video_validator.py`
- `src/services/debug_renderer.py`
- `src/schemas/analysis.py`
- `src/core/config.py`
- `src/core/exceptions.py`
- `tests/test_analyze.py`
- `tests/test_video_io_safety.py`
- `tests/test_health.py`

## Request path

```text
POST /analyze (multipart video)
  → FastAPI multipart parsing
  → route checks unexpected form fields
  → temporary_upload: extension check, chunked persistence, byte counter
  → VideoValidator: decoder and metadata checks
  → synchronous tracker/analyzer work
  → reproducibility metadata and source-video debug copy
  → response mapping, optional debug rendering, public diagnostics
  → temporary-upload cleanup
```

The route returns public union response models for completed, ambiguous, and selected
non-completed outcomes. Expected `AnalysisError` exceptions become a 422 response
with `{"detail": {"error": ...}}`; unexpected exceptions become a 500 response with
the generic message `Analysis failed.`

## Findings

### Critical

1. **CPU- and disk-intensive work runs synchronously inside an `async` endpoint.**
   Validation, tracking/model inference, debug-source copying, camera-motion
   estimation, frame rendering, and per-frame JPEG writing all execute on the event
   loop thread. A single large or long request can delay unrelated requests;
   concurrent uploads compound CPU, GPU, file-handle, and disk pressure. There is no
   request concurrency limit, deadline, cancellation boundary, or work budget.

2. **Debug artifact generation has an unbounded per-request disk footprint.** Every
   request copies the uploaded source and writes a debug MP4 plus one JPEG for every
   decoded frame. Artifacts are retained under `debug_output_dir` indefinitely, with
   no quota, lifecycle policy, cleanup job, or production enablement switch. This is
   a direct disk-exhaustion risk even when uploads meet byte and duration limits.

### High

1. **The application-level upload limit happens after multipart parsing.**
   `temporary_upload` enforces `max_upload_bytes` while it streams `UploadFile` into
   a named temporary file. However FastAPI/Starlette has already parsed the multipart
   body and may have spooled it before the endpoint executes. An upstream HTTP body
   limit and multipart parser limits are still required to make the configured limit
   a complete denial-of-service control.

2. **Content type is not validated or cross-checked.** The filename extension is
   allowlisted and OpenCV must decode the persisted file, which prevents a simple
   extension-only bypass. `UploadFile.content_type` is not inspected, though, and
   there is no explicit declared-type-versus-decoded-format policy. This produces
   inconsistent feedback for a mismatched MIME type and relies on decoder behavior.

3. **Public debug artifacts can disclose server paths and raw uploaded video.**
   `render_debug_video` returns stringified local paths. With an absolute
   `debug_output_dir`, these are absolute server filesystem paths. Even when relative,
   the response tells clients about local artifact layout. The copied source, debug
   video, and frames are stored without access control by this service. Artifact
   references should be opaque identifiers or authenticated download URLs, and debug
   generation should default off in production.

4. **Client-visible errors do not consistently carry a request identifier.**
   Successful and non-completed domain responses include `analysis_id`, but 422 and
   500 HTTP errors do not. Several logs also omit it (`analysis_validation_failed` and
   the route-level `analysis_failed`). Clients cannot reliably correlate errors with
   support records or server logs.

### Medium

1. **HTTP error semantics are coarse and inconsistent.** Oversized uploads,
   unsupported filenames, decoder failures, invalid metadata, and unexpected multipart
   fields are all mapped to 422. Framework validation errors retain FastAPI's native
   `detail` list, while application failures use `detail.error`; non-completed analysis
   states return HTTP 200 with a different response shape. Clients need a documented
   stable error envelope before any status-code refinement.

2. **Temporary-file cleanup can mask the original request failure.** The context
   manager closes the upload and unlinks the path in `finally`; a close or unlink error
   is not handled separately. On Windows, an outstanding OpenCV or copy handle can
   make unlink fail and replace the original validation/analysis exception. There is
   no cleanup warning, retry, or orphan-file metric.

3. **Debug writing is performed under the request deadline.** `copyfile` occurs
   before temporary upload removal, and rendering occurs while constructing the
   completed response. Slow storage turns an otherwise completed analysis into a slow
   request even though debug output is non-essential.

4. **Production diagnostics are overly detailed by default.** Completed responses
   include model version, reproducibility metadata (including video hash and git
   commit), per-stage timing, threshold maps, candidate statistics, trajectory
   diagnostics, and debug artifact paths. These are useful in controlled debugging but
   expose implementation detail and can create large responses. No production
   diagnostic policy or authenticated debug mode exists.

5. **No server-side request timeout or cancellation policy exists.** A client
   disconnect does not establish a cancellation boundary around synchronous OpenCV or
   model work. The service can continue consuming resources after the caller leaves.

6. **Module-level `app = create_app()` constructs services at import.** This retains
   the public entry point but creates the application and service graph when importing
   `main`. Model loading is lazy in the detector adapter, so this is not inference,
   but import-time configuration/logging failures remain possible and `app` imports use
   production-default settings.

### Low

1. **Filename handling is safe for local temporary persistence.** Only the lowercase
   suffix from `Path(upload.filename)` is used; the generated `NamedTemporaryFile`
   name is not influenced by the supplied directory or basename. Filename traversal
   therefore does not control the temporary path.

2. **The upload writer is chunked, not whole-file buffered by application code.** It
   reads one MiB chunks and rejects once the application counter crosses the configured
   maximum, subject to the multipart parsing limitation.

3. **Unexpected internal errors are not leaked to clients.** Generic 500 detail is
   appropriate. Expected `AnalysisError` messages are exposed and should remain
   audited if messages begin including paths, codec internals, or model details.

4. **The public API remains backward compatible during this review.** No response
   field, status code, error envelope, or artifact contract was changed.

## Temporary storage and cleanup assessment

The upload context creates a generated file with an allowlisted suffix, writes it in
bounded chunks, closes the upload, and calls `unlink(missing_ok=True)` in `finally`.
The validator releases its `VideoCapture` in a `finally` block. These are sound local
ownership practices. The remaining gap is handling cleanup failures independently and
observing orphaned files, especially on Windows.

The debug artifact directory is a separate lifecycle from temporary upload storage.
It is not cleaned up and is the more material storage risk.

## Concurrency and queue decision

A background queue is **not introduced in this phase**, as required. It becomes
necessary when any of the following is true:

- normal analysis latency exceeds the web request timeout or user-facing SLA;
- more than one concurrent analysis causes unacceptable model/GPU contention;
- debug rendering or artifact persistence must be retried independently of analysis;
- jobs need durable admission control, cancellation, quotas, progress, or retrieval
  after a client disconnects.

Before introducing a queue, add explicit process-local concurrency limits and an
upstream request-body limit; define immutable job input ownership and an artifact
retention policy. A queue should not be used solely to hide synchronous work without
those lifecycle controls.

## Test assessment and gaps

Existing tests cover valid upload flow, unexpected multipart fields, decoder failure,
temporary-file removal, unsafe filename rejection, and application-level oversize
rejection. Major missing deterministic coverage:

- declared MIME mismatch against decodable and non-decodable payloads;
- HTTP status and envelope assertions for each expected failure category;
- `analysis_id` or request-ID availability on 422 and 500 responses;
- cleanup failure behavior and orphan-file observability;
- debug artifacts never exposing absolute paths;
- artifact retention/quota behavior;
- concurrent request admission and event-loop responsiveness;
- disconnect/cancellation cleanup;
- production versus debug diagnostic visibility.

## Changes made

No production or test code was changed. This report is the only added file.

## Remaining risks

The endpoint is suitable for controlled, low-concurrency use with trusted storage,
but not yet for untrusted high-concurrency production traffic. Resource admission,
artifact retention, error correlation, and a public diagnostic policy must be added
before exposing it broadly.
