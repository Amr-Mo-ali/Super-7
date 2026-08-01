"""Explicit errors translated at the HTTP boundary."""


class AnalysisError(Exception):
    """Base exception for expected analysis-request failures."""


class UploadTooLargeError(AnalysisError):
    """Raised when an upload exceeds the configured byte limit."""


class InvalidVideoError(AnalysisError):
    """Raised when a video cannot satisfy the validation contract."""


class InvalidTargetBoxError(AnalysisError):
    """Raised when optional target-box fields are incomplete or invalid."""


class InvalidRequestError(AnalysisError):
    """Raised when a request includes fields outside the public contract."""


class RealDetectorNotConfiguredError(AnalysisError):
    """Raised when production analysis has no real detector/tracker adapter."""


class ModelLoadingError(AnalysisError):
    """Raised when the configured detection model cannot be loaded."""


class InvalidFrameError(AnalysisError):
    """Raised when detector input is not a valid decoded image frame."""


class InferenceError(AnalysisError):
    """Raised when model inference fails."""
