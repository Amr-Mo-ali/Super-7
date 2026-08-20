"""Real spawn proof for the unused MVP-2B2 child entry point."""

import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from process_entrypoint_spawn_support import (
    initialize_fake_analysis_child,
    run_fake_child_analysis,
)

from core.config import Settings
from schemas.analysis import NonCompletedResponse
from services.process_contracts import (
    CHILD_ANALYSIS_SCHEMA_VERSION,
    ChildAnalysisRequest,
    ChildAnalysisSuccess,
    validate_child_result,
)


def test_spawned_child_reuses_one_process_and_cleans_artifacts(tmp_path: Path) -> None:
    video_root = tmp_path / "videos"
    video_root.mkdir()
    (video_root / "safe.mp4").touch()
    debug_root = tmp_path / "debug"
    settings = Settings(video_storage_root=str(video_root), debug_output_dir=str(debug_root))
    first = ChildAnalysisRequest("spawn-one", "video-1", "player-1", "safe.mp4")
    second = ChildAnalysisRequest("spawn-two", "video-2", "player-2", "safe.mp4")
    parent_pid = os.getpid()
    context = multiprocessing.get_context("spawn")
    executor = ProcessPoolExecutor(
        max_workers=1,
        mp_context=context,
        initializer=initialize_fake_analysis_child,
        initargs=(settings,),
    )
    child_pid: int | None = None
    try:
        first_pid, first_result = executor.submit(run_fake_child_analysis, first).result(timeout=10)
        second_pid, second_result = executor.submit(run_fake_child_analysis, second).result(
            timeout=10
        )
        child_pid = first_pid
        assert first_pid != parent_pid
        assert second_pid == first_pid
        assert isinstance(first_result, ChildAnalysisSuccess)
        assert isinstance(second_result, ChildAnalysisSuccess)
        validated_first = validate_child_result(
            "spawn-one", CHILD_ANALYSIS_SCHEMA_VERSION, first_result
        )
        validated_second = validate_child_result(
            "spawn-two", CHILD_ANALYSIS_SCHEMA_VERSION, second_result
        )
        assert isinstance(validated_first, NonCompletedResponse)
        assert isinstance(validated_second, NonCompletedResponse)
        assert validated_first.analysis_id == "spawn-one"
        assert validated_second.analysis_id == "spawn-two"
        assert not (debug_root / "spawn-one").exists()
        assert not (debug_root / "spawn-two").exists()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    assert child_pid is not None
    assert not any(
        child.pid == child_pid and child.is_alive() for child in multiprocessing.active_children()
    )
