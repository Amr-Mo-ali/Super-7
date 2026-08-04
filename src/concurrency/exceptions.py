"""Operational exceptions for local concurrency coordination."""


class AdmissionRejectedError(Exception):
    """Raised when local analysis capacity is exhausted before execution begins."""
