"""Scoped capture support for the explicit application logging boundary."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from _pytest.logging import LogCaptureFixture


@contextmanager
def capture_application_logs(caplog: LogCaptureFixture) -> Iterator[None]:
    """Attach pytest capture directly to the non-propagating application logger."""
    application_logger = logging.getLogger("football_analysis")
    attached = caplog.handler not in application_logger.handlers
    if attached:
        application_logger.addHandler(caplog.handler)
    try:
        yield
    finally:
        if attached:
            application_logger.removeHandler(caplog.handler)
