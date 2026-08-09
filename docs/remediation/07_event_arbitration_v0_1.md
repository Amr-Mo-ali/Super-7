# Event Arbitration V0.1

The pass and shot detectors independently generate accepted candidates, so one underlying trajectory could previously appear twice in Public Rating V2. Arbitration is a downstream, deterministic representation layer: it never changes, deletes, or reruns detector output, and does not confirm football ground truth.

V0.1 groups pass/shot candidates and same-type pass or shot candidates when inclusive temporal overlap (`intersection / shortest duration`) is at least .80, start/end frames are within 2 frames, release frames within 1 frame, and compact distance evidence is within 15%. Adjacent frame ranges have zero overlap. Invalid ranges are not public events.

Same-type duplicates retain the candidate ranked by confidence, trajectory quality, valid frame duration, then lexicographically smaller ID. Pass/shot conflicts compare event-specific strength: receiver presence, pass confidence, and trajectory quality for passes; preparation, release, follow-through, shot confidence, and trajectory quality for shots. A .15 margin and event-specific quality gate are required to classify. Otherwise one `ambiguous` untyped event preserves every source ID; it is not counted as both a pass and shot.

Arbitration confidence describes classification certainty separately from candidate event confidence. V2 exposes one sorted unified timeline, source IDs, candidate types, limitations, arbitration confidence, deduplicated counts, ambiguous count, total public count, and `event_arbitration_v0.1`.

Game Intelligence receives classified deduplicated pass/shot counts. Ambiguous pass/shot candidates produce no pass/shot contribution and add `ambiguous_pass_shot_event`; they never count twice.

The accounting contract is source ownership: each valid source candidate belongs to exactly one public event or suppressed duplicate list. `raw_candidate_count` retains all inputs, while `public_event_count`, suppressed duplicates, and ambiguity are reported independently because an ambiguous event has multiple sources.

Supported conflicts are pass-vs-shot and duplicate/strongly-overlapping same-type pass or shot candidates. Controlled movement/dribble/loss cross-type conflicts, possession chains, tactical context, goal geometry, calibrated pitch coordinates, and labelled-data calibration remain deferred. Future work should calibrate rules with human-labelled event datasets.
