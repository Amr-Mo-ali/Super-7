"""Standard-library logging access for application components."""

import logging

_APPLICATION_LOGGER_NAME = "football_analysis"
_OWNED_HANDLER_MARKER = "_football_analysis_owned_handler"
_FORMAT = "%(levelname)s %(name)s %(message)s"


def configure_logging() -> None:
    """Configure the application logger without changing global logging state."""
    application_logger = logging.getLogger(_APPLICATION_LOGGER_NAME)
    application_logger.setLevel(logging.INFO)
    application_logger.propagate = False

    owned_handler = next(
        (
            handler
            for handler in application_logger.handlers
            if getattr(handler, _OWNED_HANDLER_MARKER, False)
        ),
        None,
    )
    if owned_handler is not None:
        owned_handler.setLevel(logging.INFO)
        owned_handler.setFormatter(logging.Formatter(_FORMAT))
        return

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_FORMAT))
    setattr(handler, _OWNED_HANDLER_MARKER, True)
    application_logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a named standard-library logger without application global state."""
    return logging.getLogger(name)
