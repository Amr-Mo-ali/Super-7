"""Standard-library logging access for application components."""

import logging


def get_logger(name: str) -> logging.Logger:
    """Return a named standard-library logger without application global state."""
    return logging.getLogger(name)
