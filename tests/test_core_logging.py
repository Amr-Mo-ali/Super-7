"""Focused tests for application logging configuration."""

import logging
from io import StringIO

import pytest
from application_log_capture import capture_application_logs
from pytest import MonkeyPatch

from core.logging import configure_logging, get_logger


def test_configure_logging_enables_effective_info_for_application_namespace(
    monkeypatch: MonkeyPatch,
) -> None:
    application_logger = logging.getLogger("football_analysis")
    monkeypatch.setattr(application_logger, "level", logging.WARNING)

    configure_logging()

    assert logging.getLogger("football_analysis.api").getEffectiveLevel() == logging.INFO


def test_configure_logging_preserves_root_and_external_handlers(
    monkeypatch: MonkeyPatch,
) -> None:
    root_logger = logging.getLogger()
    third_party_logger = logging.getLogger("uvicorn")
    application_logger = logging.getLogger("football_analysis")
    root_handler = logging.NullHandler()
    application_handler = logging.NullHandler()
    root_logger.addHandler(root_handler)
    application_logger.addHandler(application_handler)
    monkeypatch.setattr(root_logger, "level", logging.ERROR)
    monkeypatch.setattr(third_party_logger, "level", logging.WARNING)

    try:
        configure_logging()

        assert root_logger.level == logging.ERROR
        assert third_party_logger.level == logging.WARNING
        assert root_handler in root_logger.handlers
        assert application_handler in application_logger.handlers
    finally:
        root_logger.removeHandler(root_handler)
        application_logger.removeHandler(application_handler)


def test_configure_logging_owns_one_handler_and_prevents_root_duplicates() -> None:
    application_logger = logging.getLogger("football_analysis")
    root_logger = logging.getLogger()
    root_stream = StringIO()
    root_handler = logging.StreamHandler(root_stream)
    root_logger.addHandler(root_handler)

    try:
        configure_logging()
        configure_logging()

        owned_handlers = [
            handler
            for handler in application_logger.handlers
            if getattr(handler, "_football_analysis_owned_handler", False)
        ]
        assert application_logger.level == logging.INFO
        assert application_logger.propagate is False
        assert len(owned_handlers) == 1
        assert owned_handlers[0].level == logging.INFO
        get_logger("football_analysis.api").info("parent-event")
        assert root_stream.getvalue() == ""
    finally:
        root_logger.removeHandler(root_handler)


def test_configure_logging_emits_child_info_without_adding_context() -> None:
    configure_logging()
    application_logger = logging.getLogger("football_analysis")
    handler = next(
        handler
        for handler in application_logger.handlers
        if getattr(handler, "_football_analysis_owned_handler", False)
    )
    assert isinstance(handler, logging.StreamHandler)
    stream = StringIO()
    original_stream = handler.stream
    handler.stream = stream
    try:
        get_logger("football_analysis.child").info("analysis_child_initialized child_pid=%s", 123)
    finally:
        handler.stream = original_stream

    assert (
        stream.getvalue()
        == "INFO football_analysis.child analysis_child_initialized child_pid=123\n"
    )


def test_caplog_captures_application_records_when_attached_to_the_boundary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging()
    caplog.set_level(logging.INFO, logger="football_analysis")

    with capture_application_logs(caplog):
        get_logger("football_analysis.api").info("application-capture-proof")

    assert [record.getMessage() for record in caplog.records] == ["application-capture-proof"]


def test_get_logger_returns_the_named_stdlib_logger() -> None:
    logger = get_logger("football_analysis.callback")

    assert logger is logging.getLogger("football_analysis.callback")
