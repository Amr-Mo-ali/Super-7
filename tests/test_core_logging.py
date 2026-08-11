"""Focused tests for application logging configuration."""

import logging

from pytest import MonkeyPatch

from core.logging import configure_logging, get_logger


def test_configure_logging_enables_effective_info_for_application_namespace(
    monkeypatch: MonkeyPatch,
) -> None:
    application_logger = logging.getLogger("football_analysis")
    monkeypatch.setattr(application_logger, "level", logging.WARNING)

    configure_logging()

    assert logging.getLogger("football_analysis.api").getEffectiveLevel() == logging.INFO


def test_configure_logging_does_not_change_root_or_third_party_levels(
    monkeypatch: MonkeyPatch,
) -> None:
    root_logger = logging.getLogger()
    third_party_logger = logging.getLogger("uvicorn")
    monkeypatch.setattr(root_logger, "level", logging.ERROR)
    monkeypatch.setattr(third_party_logger, "level", logging.WARNING)

    configure_logging()

    assert root_logger.level == logging.ERROR
    assert third_party_logger.level == logging.WARNING


def test_configure_logging_is_idempotent_and_does_not_add_handlers() -> None:
    application_logger = logging.getLogger("football_analysis")
    handler_count = len(application_logger.handlers)

    configure_logging()
    configure_logging()

    assert application_logger.level == logging.INFO
    assert len(application_logger.handlers) == handler_count


def test_get_logger_returns_the_named_stdlib_logger() -> None:
    logger = get_logger("football_analysis.callback")

    assert logger is logging.getLogger("football_analysis.callback")
