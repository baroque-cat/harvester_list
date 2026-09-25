#!/usr/bin/env python3

"""
Core Exception Classes

This module provides essential exception classes for the application.
"""

from typing import Optional

from .enums import ErrorReason


class BaseError(Exception):
    """Base exception class for the application"""

    def __init__(
        self,
        message: str,
        reason: ErrorReason = ErrorReason.UNKNOWN,
        cause: Optional[Exception] = None,
    ):
        super().__init__(message)
        self.message = message
        self.reason = reason
        self.cause = cause

    def is_retryable(self) -> bool:
        """Check if error is retryable based on reason"""
        return self.reason.is_retryable()


class NetworkError(BaseError):
    """Network-related errors"""

    def __init__(self, message: str, reason: ErrorReason = ErrorReason.NETWORK_ERROR, **kwargs):
        super().__init__(message=message, reason=reason, **kwargs)


class TransientFetchError(NetworkError, ConnectionError):
    """No usable answer was obtained for a fetch request.

    Raised by the client boundary (``search.client``) when an empty outcome is
    a *failure-empty* rather than a legitimate zero: transport exception after
    retries exhausted, local limiter suppression, or a blank payload.  It also
    subclasses the builtin :class:`ConnectionError` so the existing stage retry
    policy (which retries ``ConnectionError``/``TimeoutError``) treats it as a
    retryable transient failure.
    """

    def __init__(self, message: str, reason: ErrorReason = ErrorReason.NETWORK_ERROR, **kwargs):
        super().__init__(message=message, reason=reason, **kwargs)


class RateLimitDeferral(NetworkError):
    """A fetch was refused on a published rate limit before completing.

    Deliberately a *sibling* of :class:`TransientFetchError` rather than a
    subclass of the builtin :class:`ConnectionError`: ``RetryCore.should_retry_error``
    retries ``ConnectionError``/``TimeoutError``, and no existing
    ``except TransientFetchError`` handler may collapse a deferral into a
    failure-empty.  A deferral is neither an answer nor a task-level fault
    (failure-handling S18).

    ``wait_s`` is the bounded, published resumption wait; ``stage_pause`` marks
    an actor-scoped (abuse/secondary) refusal that should pause the whole stage,
    not just re-enqueue this task.
    """

    def __init__(
        self,
        message: str,
        wait_s: float = 0.0,
        stage_pause: bool = False,
        **kwargs,
    ):
        super().__init__(message=message, **kwargs)
        self.wait_s = float(wait_s)
        self.stage_pause = bool(stage_pause)


class ValidationError(BaseError):
    """Input validation errors"""

    def __init__(self, message: str, field: Optional[str] = None, **kwargs):
        super().__init__(
            message=message,
            reason=ErrorReason.BAD_REQUEST,
            **kwargs,
        )
        self.field = field


# Additional exception classes for core functionality
class CoreException(BaseError):
    """Core system exception"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class BusinessLogicError(BaseError):
    """Business logic errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class ProcessingError(BaseError):
    """Processing-related errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class RetrievalError(BaseError):
    """Data retrieval errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)


class ConfigurationError(BaseError):
    """Configuration-related errors"""

    def __init__(self, message: str, **kwargs):
        super().__init__(message=message, **kwargs)
