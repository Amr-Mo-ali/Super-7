"""Standard-library logging access for application components."""

import logging


def configure_logging() -> None:
    """Enable INFO records for application loggers without adding handlers."""
    logging.getLogger("football_analysis").setLevel(logging.INFO)


def get_logger(name: str) -> logging.Logger:
    """Return a named standard-library logger without application global state."""
    return logging.getLogger(name)
