# Route lifecycle characterization

The integrated route previously lacked a factory seam for operational singleton observation and a deterministic capacity-exhaustion characterization. `create_app` now accepts explicit validator/lifecycle injection and exposes the composed singleton identities on `app.state`.

Focused tests prove a held admission permit rejects a second route request using the existing `failed` response shape, does not invoke its tracker, increments rejection accounting once, and returns active permits to zero. They also prove separate app instances own independent operational graphs. Fake validators and trackers prevent model, OpenCV, GPU, and network execution.

The admission test exposed and corrected an existing failed-response diagnostics default (`None` versus required dictionary) without changing the response schema or shape.

Unresolved gaps: concurrent in-process ASGI blocking harness, event-loop responsiveness, disconnect/deadline/shutdown signals, render-output staging, and per-request identity observation through route execution. Next phase: deterministic concurrency/lifecycle tests using an explicit ASGI lifespan-safe harness.
