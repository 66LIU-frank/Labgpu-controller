class LabGPUError(Exception):
    """Base error for user-facing LabGPU failures."""


class NotFoundError(LabGPUError):
    """Raised when a run cannot be resolved."""
