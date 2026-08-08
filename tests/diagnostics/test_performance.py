"""Deterministic tests for opt-in benchmark accounting."""

from diagnostics.performance import PerformanceCollector, current_collector, use_collector


def test_collector_records_stages_and_safe_derived_values() -> None:
    collector = PerformanceCollector("cpu")
    collector.start("analysis-1")
    with use_collector(collector), collector.stage("total_pipeline"):
        assert current_collector() is collector
        with collector.stage("player_detection"):
            pass
    collector.set_video(2.0, 20, 100)
    result = collector.finish()
    profile = result.as_dict()

    assert profile["analysis_id"] == "analysis-1"
    assert result.stages_ns["player_detection"] >= 0
    assert profile["effective_processed_fps"] is not None
    assert current_collector() is None


def test_zero_duration_and_unavailable_rss_are_safe() -> None:
    collector = PerformanceCollector("cpu")
    collector.start("analysis-2")
    collector.set_video(0.0, 0, 0)
    profile = collector.finish().as_dict()

    assert profile["realtime_factor"] is None
    assert profile["processing_ms_per_video_second"] is None
    assert profile["effective_processed_fps"] is None
