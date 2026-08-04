"""Deterministic unit tests for request-owned artifact lifecycle management."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread

import pytest

from diagnostics.artifacts import (
    ArtifactManager,
    ArtifactPathError,
    ArtifactQuotaError,
    ArtifactSession,
    ArtifactStateError,
)


def test_creates_and_finalizes_one_request_owned_artifact() -> None:
    with TemporaryDirectory() as directory:
        session = ArtifactManager(Path(directory), max_session_bytes=10).create_session("request-1")
        artifact = session.reserve("debug.mp4", 5)
        temporary = session.create(artifact)
        temporary.write_bytes(b"video")

        final = session.finalize(artifact)

        assert final.name == "debug.mp4"
        assert final.read_bytes() == b"video"
        assert session.artifacts() == (final,)


def test_cleanup_removes_non_retained_artifacts_and_is_idempotent() -> None:
    with TemporaryDirectory() as directory:
        session = ArtifactManager(Path(directory), max_session_bytes=10).create_session("request-2")
        artifact = session.reserve("frame.jpg", 5)
        session.create(artifact).write_bytes(b"frame")
        final = session.finalize(artifact)

        first = session.cleanup()
        second = session.cleanup()

        assert first.errors == ()
        assert second.errors == ()
        assert not final.exists()


def test_cleanup_after_primary_failure_removes_partial_artifact() -> None:
    with TemporaryDirectory() as directory:
        session = ArtifactManager(Path(directory), max_session_bytes=10).create_session("request-3")
        artifact = session.reserve("render.mp4", 5)
        partial = session.create(artifact)

        try:
            raise RuntimeError("primary failure")
        except RuntimeError:
            cleanup = session.cleanup()

        assert cleanup.errors == ()
        assert not partial.exists()


def test_partial_artifacts_are_not_valid_output() -> None:
    with TemporaryDirectory() as directory:
        session = ArtifactManager(Path(directory), max_session_bytes=10).create_session("request-4")
        artifact = session.reserve("debug.mp4", 5)

        with pytest.raises(ArtifactStateError, match="created"):
            session.finalize(artifact)
        assert session.artifacts() == ()


def test_retention_prunes_oldest_finalized_session_deterministically() -> None:
    with TemporaryDirectory() as directory:
        manager = ArtifactManager(Path(directory), max_session_bytes=10, retained_sessions=1)
        first = _finalized_session(manager, "request-5a", "first.mp4")
        first.retain()
        first_path = first.artifacts()[0]
        first.cleanup()
        second = _finalized_session(manager, "request-5b", "second.mp4")
        second.retain()
        second_path = second.artifacts()[0]
        second.cleanup()

        assert not first_path.exists()
        assert second_path.exists()


def test_quota_exhaustion_prevents_additional_reservations() -> None:
    with TemporaryDirectory() as directory:
        session = ArtifactManager(Path(directory), max_session_bytes=5).create_session("request-6")
        session.reserve("first.bin", 4)

        with pytest.raises(ArtifactQuotaError, match="quota"):
            session.reserve("second.bin", 2)


@pytest.mark.parametrize("name", ["../escape.mp4", "nested/frame.jpg", "..", ""])
def test_path_traversal_is_rejected(name: str) -> None:
    with TemporaryDirectory() as directory:
        session = ArtifactManager(Path(directory), max_session_bytes=10).create_session("request-7")

        with pytest.raises(ArtifactPathError):
            session.reserve(name, 1)


def test_concurrent_request_sessions_are_isolated() -> None:
    with TemporaryDirectory() as directory:
        manager = ArtifactManager(Path(directory), max_session_bytes=20)
        barrier = Barrier(2)
        paths: list[Path] = []

        def create(request_id: str) -> None:
            session = manager.create_session(request_id)
            artifact = session.reserve("debug.mp4", 10)
            barrier.wait()
            session.create(artifact).write_bytes(request_id.encode())
            paths.append(session.finalize(artifact))

        first = Thread(target=create, args=("request-a",))
        second = Thread(target=create, args=("request-b",))
        first.start()
        second.start()
        first.join()
        second.join()

        assert len(paths) == 2
        assert paths[0].parent != paths[1].parent
        assert {path.read_bytes() for path in paths} == {b"request-a", b"request-b"}


def _finalized_session(manager: ArtifactManager, request_id: str, name: str) -> ArtifactSession:
    session = manager.create_session(request_id)
    artifact = session.reserve(name, 5)
    session.create(artifact).write_bytes(b"done")
    session.finalize(artifact)
    return session
