"""Phase 0 safety policy tests without detector or video execution."""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from api.request_lifecycle import RequestLifecycle
from api.routes import _public_debug_artifact_references
from concurrency.admission import AdmissionController
from concurrency.executor import AnalysisExecutor
from config.debug import DebugSettings
from core.config import Settings
from diagnostics.artifacts import ArtifactManager


def test_debug_artifacts_are_disabled_by_default() -> None:
    settings = Settings()
    assert settings.debug == DebugSettings()
    assert not settings.debug.enabled
    assert not settings.debug.save_video
    assert not settings.debug.save_frames


def test_debug_settings_explicitly_enable_requested_media_only() -> None:
    settings = Settings(debug=DebugSettings(enabled=True, save_video=True, retained_sessions=1))
    assert settings.debug.enabled
    assert settings.debug.save_video
    assert not settings.debug.save_frames
    assert settings.debug.retained_sessions == 1


def test_failed_analysis_cleans_its_request_artifact_session() -> None:
    with TemporaryDirectory() as directory:
        manager = ArtifactManager(Path(directory), max_session_bytes=10, retained_sessions=0)
        lifecycle = RequestLifecycle(AdmissionController(1), AnalysisExecutor(), manager)

        def fail(_: object, session: object) -> None:
            del session
            raise RuntimeError("synthetic failure")

        try:
            asyncio.run(lifecycle.execute_with_artifacts("failed-request", fail))
        except RuntimeError:
            pass
        else:
            raise AssertionError("The synthetic analysis failure was not propagated.")
        assert not (Path(directory) / "failed-request").exists()


def test_retention_policy_keeps_only_configured_request_count() -> None:
    with TemporaryDirectory() as directory:
        manager = ArtifactManager(Path(directory), max_session_bytes=10, retained_sessions=1)
        first = manager.create_session("request-one")
        first.retain()
        first.cleanup()
        second = manager.create_session("request-two")
        second.retain()
        second.cleanup()
        assert not (Path(directory) / "request-one").exists()
        assert (Path(directory) / "request-two").exists()


def test_public_debug_references_never_serialize_local_paths() -> None:
    public = _public_debug_artifact_references(
        {"debug_video": r"E:\super7\debug\request\debug_video.mp4"}
    )
    assert public == {"debug_video": "debug_video.mp4"}
    assert "E:" not in public["debug_video"]
