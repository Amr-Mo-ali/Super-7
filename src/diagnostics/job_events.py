"""Concise, safe lifecycle observations for the controlled-concurrency baseline."""

import logging
from collections.abc import Mapping


def log_job_event(
    logger: logging.Logger,
    event: str,
    *,
    analysis_id: str,
    video_id: str,
    player_id: str,
    queue_depth: int,
    queue_capacity: int,
    active_analysis_count: int,
    max_active_analyses: int,
    accepting: bool,
    fields: Mapping[str, object] | None = None,
) -> None:
    """Log safe job identifiers and bounded queue observations without request content."""
    suffix = "" if not fields else " " + " ".join(f"{key}=%s" for key in sorted(fields))
    values: list[object] = [
        analysis_id,
        video_id,
        player_id,
        queue_depth,
        queue_capacity,
        active_analysis_count,
        max_active_analyses,
        accepting,
    ]
    if fields:
        values.extend(fields[key] for key in sorted(fields))
    logger.info(
        "%s analysis_id=%s video_id=%s player_id=%s queue_depth=%s queue_capacity=%s "
        "active_analysis_count=%s max_active_analyses=%s accepting=%s" + suffix,
        event,
        *values,
    )
