"""Typed domain exceptions for v2 fantasy-sleeper-ai."""

from __future__ import annotations
from typing import Optional


class FantasyAIError(Exception):
    """Base exception for all fantasy AI errors."""
    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.detail = detail


class DataValidationError(FantasyAIError):
    """Raised when data payload fails validation or schema rules."""
    pass


class UpstreamSourceError(FantasyAIError):
    """Raised when an external API or local catalog is unavailable, corrupt, or unparseable."""
    def __init__(self, message: str, source_id: Optional[str] = None, detail: Optional[str] = None):
        super().__init__(message, detail=detail)
        self.source_id = source_id


class ReasonerExecutionError(FantasyAIError):
    """Raised when Gemini reasoner execution fails or response cannot be parsed/grounded."""
    pass


class SurvivorGateError(FantasyAIError):
    """Raised when pre-LLM fail-closed survivor gates are tripped."""
    def __init__(self, message: str, status: str = "failed", detail: Optional[str] = None):
        super().__init__(message, detail=detail)
        self.status = status
