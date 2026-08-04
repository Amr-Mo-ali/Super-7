# Video I/O and resource-safety review — Phase 4

Scope: uploaded files, temporary files, OpenCV readers/writers, frame iteration,
metadata, cleanup, artifact paths, and platform/cancellation safety. No analysis
algorithm was reviewed or changed. No production video inference was run.

## Changes applied

- Added deterministic tests in `tests/test_video_io_safety.py` for temporary upload
  deletion, unsupported path-like filenames, and pre-context upload-size enforcement.

## Findings

### Critical

None found.

### High

#### Debug artifacts leak absolute local paths through the successful API response

`debug_renderer.render_debug_video` returns `str(target)` and `str(frames_dir)`;
`routes._completed` assigns that mapping to `CompletedResponse.debug_artifacts`. The
paths are rooted at `Settings.debug_output_dir`, which may be absolute in deployment.
This violates the requested public-path safety boundary and discloses server layout.

#### Debug rendering has no `try/finally` around OpenCV writer/capture ownership

`render_debug_video` releases both objects only after the frame loop. An exception from
drawing, `writer.write`, or `cv2.imwrite` leaks one or both handles and can leave partial
artifacts. The route catches rendering exceptions, so the response survives, but resource
cleanup is not guaranteed.

### Medium

#### Upload path traversal is not exploitable, but filename handling is minimal

`temporary_upload` uses only `Path(upload.filename).suffix.lower()` and creates a server
temporary file with `NamedTemporaryFile`; it never joins the supplied filename to an output
directory. Traversal in the filename therefore cannot select the filesystem destination.
However, a filename such as `../../x.avi` is accepted based on its suffix; this is safe for
storage but means filename normalization/audit naming is absent.

#### OpenCV artifact writer validity is not checked

`debug_renderer` does not verify `VideoCapture.isOpened()`, positive/finite FPS, positive
dimensions, or `VideoWriter.isOpened()`. It falls back to 25 FPS for a zero-valued FPS and
can silently create an unusable output. The core upload validator performs stronger input
checks, but the renderer accepts arbitrary local source paths.

#### Per-request debug source copies are unconditional

`routes.analyze` copies the uploaded video to `debug/<analysis-id>/source_video.*` for every
request after tracking. This adds disk usage and I/O even if later stages fail or no debugging
is desired. There is no retention/cleanup policy for copied source or debug frames.

#### Frame loops are duplicated

OpenCV frame iteration occurs independently in `VideoValidator` (first-frame validation),
`DetectionOnlyPlayerTracker`, `CameraMotionEstimator`, and `debug_renderer`. This is streaming
and avoids full-memory loading, but it produces multiple sequential decodes per request.

### Low

#### Codec support is container-dependent

Allowed extensions are AVI/MKV/MOV/MP4. Actual decoding/encoding support is OpenCV build and
host codec dependent. Validation correctly rejects unopened/undecodable uploads, but does
not report codec identity. Debug output unconditionally requests `mp4v`.

#### Metadata validation is generally safe

`VideoValidator` checks suffix, existence, non-empty file, capture opening, decodable first
frame, non-zero frame count/FPS, configured duration/resolution/FPS limits, and releases the
capture in `finally`. It streams input persistence in 1 MiB chunks; no full upload is loaded
in memory.

#### Temporary-file cleanup is robust for ordinary exceptions/cancellation

`temporary_upload` uses an async context manager with `finally`, closes `UploadFile`, and
unlinks the generated temporary path. This protects normal exceptions and task cancellation
while control reaches the context manager. Process termination remains outside Python-level
cleanup guarantees.

#### Path portability

Runtime paths use `pathlib.Path`, which is Windows/Linux compatible. Docker uses Linux
`/app`; the API debug-artifact path leak is the main portability/public-contract concern.

## Output artifacts

Current artifacts are `debug/<analysis-id>/source_video.<suffix>`, `debug_video.mp4`, and
`debug_frames/frame_*.jpg`. They are created with `Path.mkdir(parents=True, exist_ok=True)`.
There is no manifest, clip export, retention policy, atomic write, or partial-output cleanup.

## Exact modified files

- `tests/test_video_io_safety.py`
- `docs/reviews/04_video_io_and_resource_safety.md`

## Remaining risks

1. Rendering failure can leak OpenCV resources/partial files.
2. Local debug paths can be returned to API clients.
3. Artifact growth is unbounded across successful requests.
4. Multiple video decode passes raise latency and I/O pressure.
5. Codec availability is not proactively validated for debug output.
