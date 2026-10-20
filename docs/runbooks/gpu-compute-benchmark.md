# Isolated GPU image and compute benchmark

This is an **offline compute-only** experiment prepared against base revision
`533319809458a94290266839bfe556794d13e505`. Record the actual benchmark
checkout commit as the source revision for each image and run. It calls the
production `initialize_analysis_child` and `run_child_analysis` functions in spawned
processes. It does not start FastAPI, make an Apex request, send a callback,
download an object, or write a production database. Keep output on a scratch
volume outside the production checkout. Do not deploy either benchmark image.
The concurrency-2/4 stages use extra spawned benchmark children with the same
entry point; they do not represent a production worker-count setting.

## What is measured

`MODEL_DEVICE` is passed to each Ultralytics `predict` call in both
`YOLOPlayerDetector` and `YOLOBallDetector`; both use the same setting. The
configured-device model-load log only records the request. For each job, the
benchmark separately records `device_requested` and, for both detectors,
`observed_execution_devices`: the device of the preprocessed tensor passed
into Ultralytics' inference call and the instantiated model backend device.
The benchmark observes these inside the spawned analysis child. Both CPU and
CUDA runs require inference evidence from both detectors and matching input
tensor and backend devices. A CUDA run also fails if CUDA is unavailable.
Missing CPU evidence, unexpected CUDA execution on a CPU run, or CPU fallback
on a CUDA run invalidates that job and the performance comparison.
The observation hook adds small Python overhead to both CPU and GPU runs;
use these results for comparison within this benchmark setup.

The child initializer constructs the tracker and lazy detector adapters. Each
detector constructs its YOLO object on first detection, and the child keeps
its component graph until that spawned process exits. ByteTrack and video
capture are recreated for each analysis. The benchmark records child PIDs
and model-load events; it calls a run **model reuse observed** only when the
same single child processes multiple successful jobs and each model has one
load event. The load event times only YOLO object construction. The first
frame inference log includes later setup and device transfer, but is not an
isolated model initialization measurement.

The existing child logs give input materialization, validation, tracking,
reproducibility hashing, target resolution, scoring, serialization and
cleanup durations. The response also has `timing` fields. Decode, ByteTrack,
selection, scoring, hashing, and debug work may remain CPU or I/O bound.
`scripts/benchmark_analysis.py` targets an older upload request and cannot
exercise this current spawned reference-based path. The older
`process_pool_mvp_smoke.py` measures HTTP admission, not completed inference.

The benchmark synchronizes CUDA before and after **whole-job** timing, because
CUDA launches are asynchronous. Existing internal stage logs are host-side
timings and are not CUDA kernel timings. No precision, batch size, frame
sampling, thresholds, image sizes, scoring, or model weights are altered.
`submit_to_result_ms` is parent-observed latency through result collection;
it includes queueing, polling and resource-sampling overhead and is not a
precise IPC measurement.

## Compatibility and package provenance

`Dockerfile.gpu-benchmark` uses the same digest-pinned Python 3.12 slim base
and uv 0.11.2 image as `Dockerfile.durable-trial`. It exports the unchanged
`uv.lock`, prunes its PyPI Torch/TorchVision CUDA 13 branch and
`opencv-python`, then installs the remaining production packages with the
lock's hashes. It installs exactly `torch==2.13.0+cu126` and
`torchvision==0.28.0+cu126` from the official CUDA 12.6 wheel index. The
official [PyTorch version table](https://pytorch.org/get-started/previous-versions/)
lists this pair for CUDA 12.6; the [torch wheel index](https://download.pytorch.org/whl/cu126/torch/)
and [torchvision wheel index](https://download.pytorch.org/whl/cu126/torchvision/)
list CPython 3.12 Linux x86_64 wheels. A compatible NVIDIA driver and
NVIDIA Container Toolkit are prerequisites; see [NVIDIA's CUDA compatibility
guide](https://docs.nvidia.com/deploy/cuda-compatibility/minor-version-compatibility.html).

The GPU wheels and **all packages installed in their resolution step** are
**not** covered by the project's lock hashes. This includes CUDA libraries,
Triton, CUDA bindings, and any missing general Torch dependencies such as
SymPy, NetworkX and setuptools. Installation uses the official index and
exact top-level versions; transitive versions come from wheel metadata.
`httpx==0.28.1` and
`httpcore==1.0.9` are also installed without hash verification because the
durable composition imports them while `pyproject.toml` places httpx in the
dev group. This mirrors `Dockerfile.durable-trial` and remains a packaging
issue to resolve before any production image. Ultralytics declares
`opencv-python`, while this project deliberately supplies only the locked
`opencv-python-headless`; a generic dependency checker may report that
metadata conflict. No lock or project dependency file is changed here.
The Docker build asserts the Torch and TorchVision local versions and the
CUDA runtime reported by Torch, but a build alone does not prove GPU access
or inference.

## Owner prerequisites

Use a Linux x86_64 host with Docker, an NVIDIA GPU, driver and Container
Toolkit. `docker run --gpus all ... nvidia-smi` must succeed. Use two
read-only input directories: one with approved player and ball `.pt` weights,
one with the approved `.mp4` inputs. Do not rely on Ultralytics auto-download.
Give the benchmark a separate writable output directory. Avoid using the
running CPU trial host or its mounts. Record GPU model, driver, host CPU/RAM,
Docker version, image IDs, and any CPU/memory limits with the results.

From a checkout containing the benchmark, these Bash commands build local
images without Compose or deployment. Replace the three example host paths first.

```bash
cd /path/to/benchmark-checkout
test -f Dockerfile.gpu-benchmark && test -f scripts/benchmark_gpu_pipeline.py
test -z "$(git status --porcelain)"
export BENCH_REV=$(git rev-parse HEAD)
export BENCH_TAG=$(git rev-parse --short=12 HEAD)
export BENCH_MODELS=/absolute/path/to/approved-models
export BENCH_VIDEOS=/absolute/path/to/approved-videos
export BENCH_OUT=/absolute/path/to/scratch-results
mkdir -p "$BENCH_OUT"
test -f "$BENCH_MODELS/player.pt" && test -f "$BENCH_MODELS/ball.pt"
docker build --pull -f Dockerfile.durable-trial -t "super7-cpu-bench:$BENCH_TAG" .
docker build --pull -f Dockerfile.gpu-benchmark -t "super7-gpu-bench:$BENCH_TAG" .
docker image inspect -f '{{.Id}}' "super7-cpu-bench:$BENCH_TAG"
docker image inspect -f '{{.Id}}' "super7-gpu-bench:$BENCH_TAG"
```

The runner requests shutdown, gives its own worker processes a bounded grace
period, escalates to kill, and records each verified exit, forced kill and
unresolved worker in `worker_cleanup`. Python 3.12's executor has no public
worker-handle API, so this benchmark tracks the handles in that executor's
private process registry. A failed liveness check is unresolved evidence.
`shutdown(wait=False)` and interpreter exit cannot provide an absolute wall
bound if OS process operations or Python finalizers hang. The host commands
below therefore bound `docker run` to 1200 seconds plus a 15-second kill
grace using GNU `timeout`, then make separately bounded checks for and removal
of that named benchmark container. A Docker daemon failure may prevent
container removal; the helper reports that failure for operator follow-up.
A timeout or failed container
cleanup is a failed run; retain its partial JSON if present.

Define this Bash helper on the Linux Docker host before the commands below:

```bash
set -euo pipefail
bench_docker_run() {
  local name="$1" status=0 remaining
  shift
  remaining=$(timeout -s KILL 15s docker ps -a --filter "name=^/${name}$" --format '{{.Names}}') || {
    echo "could not check benchmark container name: $name" >&2
    return 125
  }
  if [ -n "$remaining" ]; then
    echo "benchmark container name already exists: $name" >&2
    return 125
  fi
  timeout -s TERM -k 15s 1200s docker run --name "$name" --rm --stop-timeout 10 "$@" || status=$?
  remaining=$(timeout -s KILL 15s docker ps -a --filter "name=^/${name}$" --format '{{.Names}}') || {
    echo "could not verify benchmark container cleanup: $name" >&2
    return 125
  }
  if [ -n "$remaining" ]; then
    timeout -s KILL 15s docker rm -f "$name" >/dev/null || {
      echo "could not remove benchmark container: $name" >&2
      return 125
    }
  fi
  return "$status"
}
```

Use the same source script and settings for both images. `--network none`
blocks callbacks and model downloads. The input mounts are read-only, the
container filesystem is read-only, and only `/output` and `/tmp` are writable.
The container runs as the invoking non-root user. Use identical inputs and
settings; only `--device`, GPU exposure, image identity, and the runtime's
necessary CUDA differences may vary. Save image inspection and `nvidia-smi`
output beside the JSON without secrets.

```bash
export CPU_IMAGE_ID=$(docker image inspect -f '{{.Id}}' "super7-cpu-bench:$BENCH_TAG")
export GPU_IMAGE_ID=$(docker image inspect -f '{{.Id}}' "super7-gpu-bench:$BENCH_TAG")
bench_docker_run "super7-cpu-smoke-$$" --network none --read-only --tmpfs /tmp --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$BENCH_MODELS,dst=/models,readonly" \
  --mount "type=bind,src=$BENCH_VIDEOS,dst=/videos,readonly" \
  --mount "type=bind,src=$BENCH_OUT,dst=/output" \
  --mount "type=bind,src=$PWD/scripts/benchmark_gpu_pipeline.py,dst=/app/scripts/benchmark_gpu_pipeline.py,readonly" \
  -e HOME=/tmp -e YOLO_CONFIG_DIR=/tmp/ultralytics \
  -e VIDEO_STORAGE_ROOT=/videos -e MODEL_PATH=/models/player.pt \
  -e BALL_MODEL_PATH=/models/ball.pt -e DEBUG_ARTIFACTS_ENABLED=false \
  --entrypoint python "super7-cpu-bench:$BENCH_TAG" \
  /app/scripts/benchmark_gpu_pipeline.py --device cpu \
  --source-revision "$BENCH_REV" --image-identity "$CPU_IMAGE_ID" \
  --video video_011.mp4 --video video_014.mp4 \
  --workers 1 --jobs 2 --max-seconds 900 --output /output/cpu-smoke.json \
  > "$BENCH_OUT/cpu-stdout.log" 2>&1

bench_docker_run "super7-gpu-smoke-$$" --network none --read-only --tmpfs /tmp --gpus all \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$BENCH_MODELS,dst=/models,readonly" \
  --mount "type=bind,src=$BENCH_VIDEOS,dst=/videos,readonly" \
  --mount "type=bind,src=$BENCH_OUT,dst=/output" \
  --mount "type=bind,src=$PWD/scripts/benchmark_gpu_pipeline.py,dst=/app/scripts/benchmark_gpu_pipeline.py,readonly" \
  -e HOME=/tmp -e YOLO_CONFIG_DIR=/tmp/ultralytics \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
  -e VIDEO_STORAGE_ROOT=/videos -e MODEL_PATH=/models/player.pt \
  -e BALL_MODEL_PATH=/models/ball.pt -e DEBUG_ARTIFACTS_ENABLED=false \
  --entrypoint python "super7-gpu-bench:$BENCH_TAG" \
  /app/scripts/benchmark_gpu_pipeline.py --device cuda:0 \
  --source-revision "$BENCH_REV" --image-identity "$GPU_IMAGE_ID" \
  --video video_011.mp4 --video video_014.mp4 \
  --workers 1 --jobs 2 --max-seconds 900 --output /output/gpu-smoke.json \
  > "$BENCH_OUT/gpu-stdout.log" 2>&1
```

Run the comparison in either image with the same script and output mount:

```bash
bench_docker_run "super7-compare-smoke-$$" --network none --read-only --tmpfs /tmp --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$BENCH_OUT,dst=/output" \
  --mount "type=bind,src=$PWD/scripts/benchmark_gpu_pipeline.py,dst=/app/scripts/benchmark_gpu_pipeline.py,readonly" \
  --entrypoint python "super7-cpu-bench:$BENCH_TAG" \
  /app/scripts/benchmark_gpu_pipeline.py \
  --compare /output/cpu-smoke.json /output/gpu-smoke.json \
  --output /output/smoke-comparison.json
```

The comparison reports `inputs_compatible` for matching source, settings
(except device), model hashes, video hashes/metadata, concurrency and job
counts. `performance_comparison_valid` also requires both runs to finish,
verify normal worker exits with code zero and no cleanup errors or
termination escalation, contain exactly the expected unique successful jobs,
and show the requested execution device for both models in each job. Failed
and partial job records remain in the input reports and `diagnostic_jobs`;
`computation_successful` stays true if all analysis jobs succeeded even when
cleanup escalation makes `lifecycle_valid` and `completed` false. Their
responses remain in the run report. The comparison checks the cleanup records
itself, including unavailable exit codes, rather than trusting those flags.
The comparison writes JSON but exits nonzero when performance evidence is
invalid. It lists every
numerical and categorical response difference, including null/status and
timing differences, without applying an invented tolerance. Review the
differences; valid performance evidence does not mean outputs agree.

## Startup, subsequent jobs, and bounded concurrency

The smoke commands each start a fresh container. Record an external elapsed
time around each `bench_docker_run` (using Bash's `time` keyword), and
separately time a fresh `docker run ... --help` with the same image and mounts
as a container/Python startup probe. External elapsed includes Docker,
imports, hashing, metadata, analysis and exit, so it is **not** pure model
initialization. JSON `submit_to_result_ms` includes first child spawn plus
parent polling and sampling overhead;
`child_wall_ms` is the analysis callable alone. Job 0 is the first analysis
and job 1 is subsequent in the same container. Compare PIDs and per-child
model-load logs before describing job 1 as warm. Model-load `elapsed_ms`
times construction only; first frame inference includes lazy setup and is
reported separately. The runner records each child PID, Torch CUDA runtime,
GPU name, CUDA allocator peaks, and sampled child RSS plus `nvidia-smi`
per-PID VRAM. Parent RSS is sampled separately. The runner samples once per
loop before a poll wait of up to 0.5 seconds. `nvidia-smi` can block for up
to 3 seconds per call, so the cadence is not fixed; the JSON records actual
sample offsets. Sampled peaks are lower bounds. `nvidia-smi` per-PID VRAM may
be unavailable; keep `null` rather than
invent a value. Child logs retain all existing stage events. Keep output
outside any production Git checkout.

Here are exact fresh-container startup probes for the two images. They do not
initialize a model. Record their elapsed seconds as a separate startup/import
observation, not as the first analysis duration.

```bash
TIMEFORMAT='cpu_startup_elapsed_seconds=%R'
time bench_docker_run "super7-cpu-startup-$$" --network none --read-only --tmpfs /tmp \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$PWD/scripts/benchmark_gpu_pipeline.py,dst=/app/scripts/benchmark_gpu_pipeline.py,readonly" \
  --entrypoint python "super7-cpu-bench:$BENCH_TAG" \
  /app/scripts/benchmark_gpu_pipeline.py --output /tmp/unused.json --help
TIMEFORMAT='gpu_startup_elapsed_seconds=%R'
time bench_docker_run "super7-gpu-startup-$$" --network none --read-only --tmpfs /tmp --gpus all \
  --user "$(id -u):$(id -g)" \
  --entrypoint python "super7-gpu-bench:$BENCH_TAG" \
  /app/scripts/benchmark_gpu_pipeline.py --output /tmp/unused.json --help
```

Use `video_011.mp4` and `video_014.mp4` **only for smoke checks**. For capacity,
obtain consented representative 30–180 second 720p and 1080p videos,
including low/high frame rates, dense/multiple players, sparse drills,
occlusion, moving/static cameras, difficult lighting, and small/blurred ball
views. Keep exact bytes, model hashes, debug policy and settings between CPU
and GPU. Do not infer throughput or capacity from the two short smoke inputs.

Start representative measurement at concurrency 1. The runner caps this at
two jobs and 900 seconds. After reviewing successful stage-1 results and
available RAM/VRAM, stage 2 is an explicit opt-in (`--workers 2 --jobs 4
--allow-concurrency`); stage 4 is separately opt-in (`--workers 4 --jobs 8
--allow-concurrency`). Use the same mounts and settings in new disposable
containers, change the output filenames, and run CPU/GPU pairs for each
stage. The runner stops submitting new work on the first job failure or at
the deadline, attempts bounded shutdown of its own spawned children, reports
unresolved exits, and exits nonzero. The external container timeout bounds
the Docker invocation if Python or executor shutdown hangs. Stop
the container if the host shows OOM, driver errors, thermal throttling,
other workload interference, or insufficient VRAM. Do not increase
production workers or Compose replicas from this experiment.

For representative inputs, use the following command function after replacing
the example filenames with approved files in `$BENCH_VIDEOS`. Each invocation
starts a fresh container and writes its own JSON and child logs. Run the CPU
and GPU call at each stage with the same two filenames. Review stage 1 before
invoking stage 2, and stage 2 before stage 4.

```bash
bench_stage() {
  local label="$1" image="$2" image_id="$3" device="$4" workers="$5" jobs="$6"
  local gpu_args=() opt_in=()
  if [ "$device" = cuda:0 ]; then gpu_args=(--gpus all); fi
  if [ "$workers" -gt 1 ]; then opt_in=(--allow-concurrency); fi
  local TIMEFORMAT="${label}_external_elapsed_seconds=%R"
  time bench_docker_run "super7-${label}-$$" --network none --read-only --tmpfs /tmp \
    "${gpu_args[@]}" --user "$(id -u):$(id -g)" \
    --mount "type=bind,src=$BENCH_MODELS,dst=/models,readonly" \
    --mount "type=bind,src=$BENCH_VIDEOS,dst=/videos,readonly" \
    --mount "type=bind,src=$BENCH_OUT,dst=/output" \
    --mount "type=bind,src=$PWD/scripts/benchmark_gpu_pipeline.py,dst=/app/scripts/benchmark_gpu_pipeline.py,readonly" \
    -e HOME=/tmp -e YOLO_CONFIG_DIR=/tmp/ultralytics \
    -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    -e VIDEO_STORAGE_ROOT=/videos -e MODEL_PATH=/models/player.pt \
    -e BALL_MODEL_PATH=/models/ball.pt -e DEBUG_ARTIFACTS_ENABLED=false \
    --entrypoint python "$image" /app/scripts/benchmark_gpu_pipeline.py \
    --device "$device" --source-revision "$BENCH_REV" --image-identity "$image_id" \
    --video representative-720p-30s.mp4 --video representative-1080p-180s.mp4 \
    --workers "$workers" --jobs "$jobs" --max-seconds 900 "${opt_in[@]}" \
    --output "/output/${label}.json" > "$BENCH_OUT/${label}-stdout.log" 2>&1
}
bench_stage cpu-1 "super7-cpu-bench:$BENCH_TAG" "$CPU_IMAGE_ID" cpu 1 2
bench_stage gpu-1 "super7-gpu-bench:$BENCH_TAG" "$GPU_IMAGE_ID" cuda:0 1 2
# Only after stage 1 succeeds with resource headroom:
bench_stage cpu-2 "super7-cpu-bench:$BENCH_TAG" "$CPU_IMAGE_ID" cpu 2 4
bench_stage gpu-2 "super7-gpu-bench:$BENCH_TAG" "$GPU_IMAGE_ID" cuda:0 2 4
# Only after stage 2 succeeds with resource headroom:
bench_stage cpu-4 "super7-cpu-bench:$BENCH_TAG" "$CPU_IMAGE_ID" cpu 4 8
bench_stage gpu-4 "super7-gpu-bench:$BENCH_TAG" "$GPU_IMAGE_ID" cuda:0 4 8
```

The JSON result schema includes source/image identity, CPU/GPU device
inventory, config hash and settings, dependency versions, video/model SHA-256, dimensions, frames,
per-job requested and observed execution devices, status, serialized response,
child wall and parent-observed submit-to-result times, logs, child PIDs,
memory observations, computation and lifecycle flags, worker cleanup, stop
reason and model reuse evidence. Report CPU and
GPU runs separately. Future end-to-end evidence must additionally time
queue wait, object download, container/application startup and callback
delivery under an approved isolated integration setup; these measurements
exclude those stages by design.
