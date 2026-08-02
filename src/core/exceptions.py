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


class BallDetectorInitializationError(AnalysisError):
    """Raised when the configured ball detector cannot be initialized."""


class BallDetectionError(AnalysisError):
    """Raised when ball inference cannot produce trustworthy output."""


class BallTrackerError(AnalysisError):
    """Raised when ball tracking cannot continue safely."""


class BallProximityAnalysisError(AnalysisError):
    """Raised when ball proximity analysis receives invalid observations."""


class InteractionInputError(AnalysisError):
    """Raised when interaction analysis observations are invalid."""


class InteractionSegmentationError(AnalysisError):
    """Raised when interaction evidence cannot be segmented safely."""


class InteractionConfidenceError(AnalysisError):
    """Raised when interaction confidence settings are invalid."""


class InternalInteractionDiagnosticsError(AnalysisError):
    """Raised when interaction results violate their response invariants."""


class PhysicalScoreConfigurationError(AnalysisError):
    """Raised when physical scoring settings are invalid."""


class PhysicalScoreInputError(AnalysisError):
    """Raised when physical scoring evidence is invalid."""


class PhysicalScoreCalculationError(AnalysisError):
    """Raised when physical scoring cannot be completed."""


class InternalDiagnosticsError(AnalysisError):
    """Raised when a completed response would contain contradictory diagnostics."""


class MovementAnalysisError(AnalysisError):
    """Raised when movement analysis cannot safely produce metrics."""


class TrajectoryError(MovementAnalysisError):
    """Raised when trajectory observations are invalid."""


class SpeedCalculationError(MovementAnalysisError):
    """Raised when speed calculation cannot be completed."""
