"""Deterministic delivery tests for backend analysis callbacks."""

import asyncio
import json
import logging

import pytest
from pydantic import HttpUrl, TypeAdapter, ValidationError

from services.callback_service import CallbackPayload, CallbackService, DetailedRatings


def test_successful_callback_serializes_the_final_payload() -> None:
    delivered: list[tuple[str, bytes, float]] = []

    def transport(url: str, body: bytes, timeout: float) -> int:
        delivered.append((url, body, timeout))
        return 200

    service = _service(transport)
    assert asyncio.run(service.send_result(_url(), _payload())) is True
    assert json.loads(delivered[0][1]) == _payload().model_dump(mode="json")


def test_callback_timeout_retries_with_exponential_backoff(
    caplog: pytest.LogCaptureFixture,
) -> None:
    attempts = 0
    delays: list[float] = []

    def timeout(*_: object) -> int:
        nonlocal attempts
        attempts += 1
        raise TimeoutError("timed out")

    async def sleep(delay: float) -> None:
        delays.append(delay)

    caplog.set_level(logging.INFO, logger="test.callback")
    service = _service(timeout, sleep=sleep)
    assert asyncio.run(service.send_result(_url(), _payload())) is False
    assert attempts == 4
    assert delays == [1.0, 2.0, 4.0]
    messages = [record.getMessage() for record in caplog.records]
    assert sum("analysis_callback_attempt_finished" in message for message in messages) == 4
    assert sum("analysis_callback_retry_scheduled" in message for message in messages) == 3
    assert any(
        "analysis_callback_finished" in message
        and "callback_outcome=exhausted" in message
        and "callback_attempts=4" in message
        for message in messages
    )
    assert all("backend.example.com" not in message for message in messages)
    assert all("passes" not in message for message in messages)


def test_callback_retries_transient_failures_until_success() -> None:
    attempts = 0
    delays: list[float] = []

    def transport(*_: object) -> int:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary failure")
        return 204

    async def sleep(delay: float) -> None:
        delays.append(delay)

    service = _service(transport, sleep=sleep)
    assert asyncio.run(service.send_result(_url(), _payload())) is True
    assert attempts == 3
    assert delays == [1.0, 2.0]


def test_callback_rejects_localhost_without_transport() -> None:
    service = CallbackService(
        1,
        logging.getLogger("test.callback"),
        transport=lambda *_: (_ for _ in ()).throw(AssertionError("transport called")),
        resolver=lambda *_args, **_kwargs: [(None, None, None, None, ("127.0.0.1", 80))],
    )
    assert asyncio.run(service.send_result(_url("http://localhost/webhook"), _payload())) is False


def test_callback_failure_is_handled_without_raising() -> None:
    service = _service(lambda *_: 500)
    assert asyncio.run(service.send_result(_url(), _payload())) is False


def test_detailed_ratings_validate_nullable_bounded_future_values() -> None:
    assert DetailedRatings(
        speed_and_fitness=None,
        ball_control_and_individual_skill=0,
        passing_and_playmaking=72.5,
        shooting_and_finishing=100,
    ).model_dump(mode="json") == {
        "speed_and_fitness": None,
        "ball_control_and_individual_skill": 0.0,
        "passing_and_playmaking": 72.5,
        "shooting_and_finishing": 100.0,
        "defending_and_duels": None,
        "tactical_intelligence_and_teamwork": None,
        "positioning_and_off_ball_movement": None,
    }
    with pytest.raises(ValidationError):
        DetailedRatings(speed_and_fitness=-0.01)
    with pytest.raises(ValidationError):
        DetailedRatings(speed_and_fitness=100.01)
    with pytest.raises(ValidationError):
        DetailedRatings.model_validate({"speed_and_fitness": "not-a-number"})


def _service(
    transport: object,
    sleep: object = asyncio.sleep,
) -> CallbackService:
    return CallbackService(
        1,
        logging.getLogger("test.callback"),
        transport=transport,  # type: ignore[arg-type]
        resolver=lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
        sleep=sleep,  # type: ignore[arg-type]
    )


def _url(value: str = "https://backend.example.com/webhook") -> HttpUrl:
    return TypeAdapter(HttpUrl).validate_python(value)


def _payload() -> CallbackPayload:
    return CallbackPayload(
        request_id="request-789",
        video_id="video-123",
        player_id="player-456",
        status="completed",
        summary={"passes": 2},
        ratings={"technical": {"value": 75}},
        detailed=DetailedRatings(),
        events={"timeline": []},
    )
