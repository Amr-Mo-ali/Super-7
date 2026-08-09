# Public Rating JSON V2

V2 is the only public response from `POST /analyze`. The route maps the internal V1 analysis
result once; it does not rerun analysis. V1 remains an internal pipeline contract.

It exposes compact analysis/video/player metadata, ratings, an overall rating, summary counts, selected quality values, candidate events, limitations, warnings, and versions. It deliberately omits trajectory arrays, raw observations, interaction segments, thresholds, diagnostics, debug artifacts, and filesystem paths. Events have a uniform candidate shape with explicit seconds/pixels fields as appropriate.

Non-completed V2 responses include analysis id/status/version, safe user-facing reason, machine-readable reason code, warnings, and retryability. The intended ordinary response target is under 50 KB; no event truncation is applied by this presenter.

Frontend usage: render `ratings`, `overall`, `summary`, and compact `events`; treat unavailable
values as unavailable, not zero. Example: `curl -F video=@clip.mp4 http://localhost:8000/analyze`.

Limitations remain explicit: activity is image-space, proximity is not possession, and candidates are not confirmed actions. Next phase: **frontend dashboard integration**.
