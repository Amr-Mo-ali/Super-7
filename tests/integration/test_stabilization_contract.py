from config.football_profiles import ACTIVE_PROFILE, BALANCED_PROFILE
from core.pipeline import PipelineState


def test_balanced_profile_is_active_and_has_every_stabilized_domain() -> None:
    assert ACTIVE_PROFILE is BALANCED_PROFILE
    assert set(ACTIVE_PROFILE) == {
        "player_selection",
        "ball",
        "interaction",
        "controlled_movement",
        "dribble",
        "technical_scoring",
    }


def test_pipeline_state_machine_is_explicit_and_ordered() -> None:
    assert list(PipelineState)[0] is PipelineState.VIDEO
    assert list(PipelineState)[-1] is PipelineState.COMPLETE
