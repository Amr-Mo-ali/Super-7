"""Scoped capture support for the explicit application logging boundary."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from _pytest.logging import LogCaptureFixture


def _handler_index(logger: logging.Logger, handler: logging.Handler) -> int | None:
    return next(
        (index for index, candidate in enumerate(logger.handlers) if candidate is handler),
        None,
    )


def _restore_handler_position(
    logger: logging.Logger, handler: logging.Handler, original_index: int | None
) -> None:
    current_index = _handler_index(logger, handler)
    if current_index == original_index:
        return
    if current_index is not None:
        logger.removeHandler(handler)
    if original_index is not None:
        logger.handlers.insert(original_index, handler)


@contextmanager
def capture_application_logs(caplog: LogCaptureFixture) -> Iterator[None]:
    """Route application records to pytest capture through exactly one handler path."""
    application_logger = logging.getLogger("football_analysis")
    root_logger = logging.getLogger()
    capture_handler = caplog.handler
    application_index = _handler_index(application_logger, capture_handler)
    root_index = _handler_index(root_logger, capture_handler)

    if application_logger.propagate and root_index is not None:
        if application_index is not None:
            application_logger.removeHandler(capture_handler)
    elif application_index is None:
        application_logger.addHandler(capture_handler)
    try:
        yield
    finally:
        _restore_handler_position(application_logger, capture_handler, application_index)
        _restore_handler_position(root_logger, capture_handler, root_index)
