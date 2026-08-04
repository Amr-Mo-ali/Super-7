"""Immutable metadata captured for every analysis request."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from config.football_profiles import ACTIVE_PROFILE_NAME

PIPELINE_VERSION = "system_stabilization_v0.1"


def video_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def git_commit(root: Path) -> str | None:
    head = root / ".git" / "HEAD"
    if not head.exists():
        return None
    value = head.read_text(encoding="utf-8").strip()
    if value.startswith("ref: "):
        ref = root / ".git" / value.removeprefix("ref: ")
        return ref.read_text(encoding="utf-8").strip() if ref.exists() else None
    return value


def metadata(path: Path, model_version: str) -> dict[str, str | None]:
    return {
        "video_hash": video_hash(path),
        "configuration_profile": ACTIVE_PROFILE_NAME,
        "git_commit": git_commit(Path.cwd()),
        "model_versions": model_version,
        "timestamp": datetime.now(UTC).isoformat(),
        "pipeline_version": PIPELINE_VERSION,
    }
