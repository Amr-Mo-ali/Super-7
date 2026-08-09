"""Debug-media policy configuration, separate from public analysis behavior."""

from dataclasses import dataclass
from os import environ

from config.retention import DEFAULT_RETAINED_SESSIONS


def _flag(name: str) -> bool:
    return environ.get(name, "false").lower() == "true"


@dataclass(frozen=True, slots=True)
class DebugSettings:
    """Opt-in debug artifact policy; disabled by default."""

    enabled: bool = False
    save_video: bool = False
    save_frames: bool = False
    save_on_failure: bool = False
    retained_sessions: int = DEFAULT_RETAINED_SESSIONS

    def __post_init__(self) -> None:
        if self.retained_sessions < 0:
            raise ValueError("retained_sessions must not be negative.")

    @classmethod
    def from_environment(cls) -> "DebugSettings":
        return cls(
            enabled=_flag("DEBUG_ARTIFACTS_ENABLED"),
            save_video=_flag("DEBUG_SAVE_VIDEO"),
            save_frames=_flag("DEBUG_SAVE_FRAMES"),
            save_on_failure=_flag("DEBUG_SAVE_ON_FAILURE"),
            retained_sessions=int(
                environ.get("DEBUG_RETAINED_SESSIONS", DEFAULT_RETAINED_SESSIONS)
            ),
        )
