"""Configuration ownership and compatibility contracts for Phase 2."""

from math import inf, nan

import pytest

from config.analysis import DEFAULT_MAX_ACTIVE_ANALYSES
from config.arbitration import EVENT_ARBITRATION_VERSION, MAX_RELEASE_FRAME_DIFFERENCE
from config.debug import DebugSettings
from config.football_profiles import ACTIVE_PROFILE_NAME, threshold
from config.retention import DEFAULT_RETAINED_SESSIONS
from config.scoring import GAME_INTELLIGENCE_WEIGHTS, MAX_GAME_INTELLIGENCE_CONFIDENCE
from core.config import Settings
from services.event_arbitration.config import VERSION
from services.player_rating.config import (
    GAME_INTELLIGENCE_WEIGHTS as COMPATIBILITY_GAME_INTELLIGENCE_WEIGHTS,
)


def test_defaults_preserve_existing_runtime_behavior() -> None:
    settings = Settings()
    assert settings.max_upload_bytes == 100 * 1024 * 1024
    assert settings.analysis_shutdown_grace_seconds == 5.0
    assert settings.debug == DebugSettings(retained_sessions=DEFAULT_RETAINED_SESSIONS)
    assert DEFAULT_MAX_ACTIVE_ANALYSES == 1
    assert ACTIVE_PROFILE_NAME == "balanced"


def test_environment_overrides_are_owned_by_debug_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEBUG_ARTIFACTS_ENABLED", "true")
    monkeypatch.setenv("DEBUG_SAVE_VIDEO", "true")
    monkeypatch.setenv("DEBUG_RETAINED_SESSIONS", "2")
    monkeypatch.setenv("REQUEST_DEADLINE_SECONDS", "120")
    monkeypatch.setenv("ANALYSIS_SHUTDOWN_GRACE_SECONDS", "0.25")
    settings = Settings.from_environment()
    assert settings.debug == DebugSettings(enabled=True, save_video=True, retained_sessions=2)
    assert settings.request_deadline_seconds == 120
    assert settings.analysis_shutdown_grace_seconds == 0.25


@pytest.mark.parametrize("value", (0.0, -1.0, nan, inf))
def test_shutdown_grace_requires_a_positive_finite_value(value: float) -> None:
    with pytest.raises(ValueError, match="analysis_shutdown_grace_seconds"):
        Settings(analysis_shutdown_grace_seconds=value)


def test_validation_rejects_invalid_retention() -> None:
    with pytest.raises(ValueError, match="retained_sessions"):
        DebugSettings(retained_sessions=-1)


def test_profile_and_arbitration_thresholds_keep_existing_values() -> None:
    assert threshold("selection_margin") == 0.08
    assert MAX_RELEASE_FRAME_DIFFERENCE == 1
    assert VERSION == EVENT_ARBITRATION_VERSION


def test_scoring_constants_have_one_owner_with_compatibility_reexport() -> None:
    assert COMPATIBILITY_GAME_INTELLIGENCE_WEIGHTS is GAME_INTELLIGENCE_WEIGHTS
    assert MAX_GAME_INTELLIGENCE_CONFIDENCE == 0.65
