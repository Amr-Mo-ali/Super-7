# Phase 9.1: Performance and Resource Baseline

## Objective

Add opt-in, request-scoped measurement capability for the existing one-process,
one-active-analysis pipeline. This phase does not alter algorithms, model/tracker
ownership, admission, V2, or resource limits.

## Instrumentation architecture

`diagnostics.performance.PerformanceCollector` is an internal typed collector activated
only by `use_collector()`. It uses `perf_counter_ns()` for elapsed stages and is carried
to `asyncio.to_thread()` by Python context propagation. Without a collector, existing
tracking and route calls use their previous direct code path.

The collector records aggregated stage totals/counters, portable resource snapshots,
video metadata, artifact/response byte fields, and derived realtime values. It is not
serialized into Public Rating V2.

## Stage timing definitions

Implemented tracking totals are `frame_decode`, `player_detection`, `bytetrack_update`,
`ball_detection`, `ball_tracking`, and enclosing `tracking_total`; route timings include
`video_validation` and `player_selection`. Values are full-request aggregates, not
per-frame arrays. `frames_processed` is recorded as a counter.

The benchmark also records `total_request_ms` around the actual ASGI request. Existing
route timing remains the source for existing response timing fields; this collector
does not create a competing public timing system.

## Startup model measurements

The current composition constructs models during `main` import/application creation and
does not expose separate player/ball construction hooks. The harness records this as
`"not separated by the current composition path"`; it does not reload models merely to
benchmark them. Separate per-model startup timing remains an unimplemented measurement
capability and must be added only by a later composition-path instrumentation phase.

## Resource methodology

Snapshots use `process_time_ns()` and `threading.active_count()`. On platforms exposing
the standard `resource` module, RSS is `ru_maxrss` normalized to bytes; on platforms
without it (including the normal Windows Python path), RSS is `null`. Open-handle counts
are `null` because no portable standard-library implementation is present. CUDA data is
best-effort only when the configured device begins with `cuda`, PyTorch imports, and
CUDA is available; otherwise `gpu_enabled=false` and profiling continues.

## Benchmark harness

Run serially with real app composition and local videos:

```powershell
uv run python scripts/benchmark_analysis.py --video path\to\clip.mp4 --repeat 3 --warmup --output benchmark.json
```

It uses ASGI transport to call the real `/analyze` route, activates one collector per
run, emits deterministic JSON, and does not enable debug media. It reports minimum,
median, and maximum total request time per input. The output includes response byte size
and every measured stage/resource field.

## Benchmark environment and corpus

One serial CPU benchmark was executed on `dataset/raw/social_media/video_001.mp4` using
one warm-up and three measured runs. The generated machine-readable result is
`debug/benchmark_video_001.json`. The input is 1,112,012 bytes, duration 7.674 seconds,
and 230 decoded frames. Environment: Windows 11, CPython 3.12.13, configured `cpu`, one
Uvicorn worker, and `max_active_analyses=1`.

This is one short clip only. No medium/long corpus category has been measured, so it is
not a capacity baseline for all repository videos.

## Measured results and bottlenecks

Measured request-time summary (milliseconds): min 24,733.532; median 25,586.680; max
28,209.574. All measured requests returned HTTP 200 and response size was 5,709 bytes.

At the median run, measured player detection was 12,150.629 ms (47.49% of total
pipeline time) and ball detection was 12,114.517 ms (47.35%). Together they account for
94.84% of the 25,585.347 ms measured pipeline total. This makes player and ball
detection the top measured contributors for this clip/configuration only. The median
decode total was 349.090 ms, ByteTrack update total 301.950 ms, ball tracking total
3.425 ms, validation 53.037 ms, and selection 1.781 ms. Effective processed FPS ranged
from 8.154 to 9.299; realtime factor ranged from 3.223 to 3.676.

Uninstrumented post-tracking stages are included in total request/pipeline elapsed time
but are not separately attributed by this implementation. No claim is made about their
individual cost.

## Resource, disk, and GPU observations

The measured process thread count was 3 before and after each run. CPU time increased by
195.875 s, 202.156 s, and 218.734 s across the three measured wall-clock runs; this is
process CPU time, not a normalized utilization percentage. RSS/peak RSS and open handles
were unavailable on this Windows run and are `null` in JSON. Artifact bytes were zero
because debug media was disabled. GPU was inactive (`gpu_enabled=false`), with no CUDA
memory values. Temporary upload bytes equal the 1,112,012-byte input for every run.

The collector can record input upload bytes, artifact bytes when supplied by a caller,
response bytes, CPU time, thread count, and best-effort RSS/CUDA allocation. It does not
claim peak RSS on Windows, open handles, GPU utilization, or artifact bytes until those
values are actually available.

## Queue-sizing evidence

After representative runs, later planning may use:

```text
estimated_max_running_from_ram = floor(
  (available_ram - model_baseline - safety_margin) / peak_request_memory
)
```

This phase does not know deployment RAM, model baseline RSS, peak request memory, or
temporary-disk capacity, and therefore selects no queue or worker value.

## Regression-testing policy

Tests assert instrumentation shape, context scoping, nonnegative elapsed values, and
safe zero-duration derived metrics. They do not assert wall-clock performance. V2 output
is unaffected because the collector is internal and opt-in.

## Limitations and conclusions

Implemented measurements do not yet cover every requested route/post-processing stage,
separate model startup, portable RSS/handles, serialization time, or real benchmark
results. The only supported conclusion is that a low-overhead internal measurement path
and serial real-route harness now exist; capacity/bottleneck conclusions require recorded
representative runs.

## Exact next phase

Run the harness on a classified short/medium/long representative corpus with warm-up and
three measured repetitions where practical; then publish the generated JSON summary and
perform evidence-based capacity analysis. Do not implement a queue before that baseline.
