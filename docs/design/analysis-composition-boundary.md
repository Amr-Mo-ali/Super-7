# Analysis composition boundary

`create_analysis_components()` builds the CPU-analysis dependency graph: validation, lazy tracker/detectors, selection, feature extraction, ball/movement/interaction/technical analysis, pass/shot detection, and physical scoring. It creates no model weights, video I/O, network activity, threads, processes, or global cache.

The API parent deliberately retains FastAPI, routers, admission/lifecycle, artifact management, path resolution, downloader, queue/worker, callbacks, and lifespan handling. Those are not calculation components and callback delivery remains parent-owned.

The future child initializer in MVP-2B2 will reuse this factory to construct its own graph. No process/runtime switch exists yet; production continues with the existing in-process path. YOLO adapters remain lazy: model construction occurs only on actual detection.
