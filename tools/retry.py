#!/usr/bin/env python3

"""
Unified Retry Framework - Comprehensive Retry Logic and Strategies

This module provides a unified retry framework that combines:
- Strategy Pattern: Object-oriented retry policies for pipeline stages
- Decorator Pattern: Function decorators for automatic retry
- Core Algorithms: Shared retry logic with exponential backoff and jitter

Key Features:
- Multiple retry strategies (Fixed, Exponential, Jitter, None)
- Decorator support for sync/async functions
- Smart error classification and retry decisions
- Dynamic parameter extraction from function arguments
- Unified configuration and behavior
"""

import asyncio
import functools
import random
import time
from abc import ABC, abstractmethod
from typing import Callable, Tuple, Type

from core.exceptions import RateLimitDeferral

from .logger import get_logger

logger = get_logger("retry")


# ============================================================================
# Core Retry Algorithms and Utilities
# ============================================================================


class RetryCore:
    """Core retry algorithms and utilities shared across all retry mechanisms"""

    @staticmethod
    def should_retry_error(error: Exception, attempt: int, max_retries: int) -> bool:
        """Determine if an error should trigger a retry

        Args:
            error: Exception that occurred
            attempt: Current attempt number (0-based)
            max_retries: Maximum number of retries allowed

        Returns:
            bool: True if error should trigger retry
        """
        if attempt >= max_retries:
            return False

        # A typed rate-limit deferral is a hand-off to the stage, never a retry:
        # the decision is by TYPE, not by the words "rate limit" in the message
        # (design D5).  Must precede the text match below.
        if isinstance(error, RateLimitDeferral):
            return False

        # Retry on network and timeout errors
        if isinstance(error, (ConnectionError, TimeoutError)):
            return True

        # Retry on rate limiting
        error_str = str(error).lower()
        if "rate limit" in error_str or "too many requests" in error_str:
            return True

        # Don't retry on validation errors
        if isinstance(error, (ValueError, TypeError, KeyError)):
            return False

        return False

    @staticmethod
    def calculate_delay(
        attempt: int, base_delay: float = 1.0, multiplier: float = 2.0, max_delay: float = 30.0, jitter: bool = True
    ) -> float:
        """Calculate retry delay with exponential backoff and optional jitter

        Args:
            attempt: Current attempt number (0-based)
            base_delay: Base delay in seconds
            multiplier: Exponential multiplier
            max_delay: Maximum delay cap
            jitter: Whether to add random jitter

        Returns:
            float: Calculated delay in seconds
        """
        # Calculate exponential backoff
        delay = base_delay * (multiplier**attempt)
        delay = min(delay, max_delay)

        # Add jitter if requested
        if jitter:
            jitter_factor = 0.1
            jitter_amount = random.uniform(-jitter_factor, jitter_factor) * delay
            delay = max(0.1, delay + jitter_amount)

        return delay


# ============================================================================
# Strategy Pattern: Retry Policies for Object-Oriented Usage
# ============================================================================


class RetryPolicy(ABC):
    """Abstract base class for retry policies"""

    @abstractmethod
    def should_retry(self, attempt: int, error: Exception) -> bool:
        """Determine if task should be retried

        Args:
            attempt: Current attempt number (0-based)
            error: Exception that occurred

        Returns:
            bool: True if task should be retried
        """
        pass

    @abstractmethod
    def get_delay(self, attempt: int) -> float:
        """Get delay before next retry attempt

        Args:
            attempt: Current attempt number (0-based)

        Returns:
            float: Delay in seconds
        """
        pass


class FixedRetry(RetryPolicy):
    """Fixed delay retry policy"""

    def __init__(self, max_retries: int = 3, delay: float = 1.0):
        self.max_retries = max_retries
        self.delay = delay

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """Check if should retry with fixed policy"""
        return RetryCore.should_retry_error(error, attempt, self.max_retries)

    def get_delay(self, attempt: int) -> float:
        """Return fixed delay"""
        return self.delay


class ExponentialBackoff(RetryPolicy):
    """Exponential backoff retry policy"""

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 30.0, multiplier: float = 2.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.multiplier = multiplier

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """Check if should retry with exponential backoff"""
        return RetryCore.should_retry_error(error, attempt, self.max_retries)

    def get_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay"""
        return RetryCore.calculate_delay(attempt, self.base_delay, self.multiplier, self.max_delay, jitter=False)


class JitterBackoff(ExponentialBackoff):
    """Exponential backoff with jitter to avoid thundering herd"""

    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 1.0,
        max_delay: float = 30.0,
        multiplier: float = 2.0,
        jitter_factor: float = 0.1,
    ):
        super().__init__(max_retries, base_delay, max_delay, multiplier)
        self.jitter_factor = jitter_factor

    def get_delay(self, attempt: int) -> float:
        """Calculate exponential backoff delay with jitter"""
        return RetryCore.calculate_delay(attempt, self.base_delay, self.multiplier, self.max_delay, jitter=True)


class NoRetry(RetryPolicy):
    """No retry policy - fail immediately"""

    def should_retry(self, attempt: int, error: Exception) -> bool:
        """Never retry"""
        return False

    def get_delay(self, attempt: int) -> float:
        """No delay needed"""
        return 0.0


# ============================================================================
# Decorator Pattern: Function Decorators for Automatic Retry
# ============================================================================


def with_retry(
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    exceptions: Tuple[Type[Exception], ...] = (Exception,),
    max_delay: float = 30.0,
):
    """Decorator for automatic retry with exponential backoff using unified core

    Args:
        max_attempts: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        backoff_factor: Multiplier for delay after each failure
        jitter: Whether to add random jitter to delay
        exceptions: Tuple of exceptions to catch and retry
        max_delay: Maximum delay cap in seconds

    Returns:
        Decorated function with retry logic
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        # Use unified core for delay calculation
                        actual_delay = RetryCore.calculate_delay(attempt, delay, backoff_factor, max_delay, jitter)

                        logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed: {e}. " f"Retrying in {actual_delay:.2f}s..."
                        )
                        time.sleep(actual_delay)
                    else:
                        logger.error(f"All {max_attempts} attempts failed. Last error: {e}")

            # Re-raise the last exception if all attempts failed
            raise last_exception

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        # Use unified core for delay calculation
                        actual_delay = RetryCore.calculate_delay(attempt, delay, backoff_factor, max_delay, jitter)

                        logger.warning(
                            f"Attempt {attempt + 1}/{max_attempts} failed: {e}. " f"Retrying in {actual_delay:.2f}s..."
                        )
                        await asyncio.sleep(actual_delay)
                    else:
                        logger.error(f"All {max_attempts} attempts failed. Last error: {e}")

            # Re-raise the last exception if all attempts failed
            raise last_exception

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def retry_on_exception(
    exception_types: Tuple[Type[Exception], ...],
    max_attempts: int = 3,
    delay: float = 1.0,
    backoff_factor: float = 2.0,
    jitter: bool = True,
    max_delay: float = 30.0,
):
    """Retry decorator for specific exception types

    Args:
        exception_types: Tuple of exception types to retry on
        max_attempts: Maximum number of retry attempts
        delay: Initial delay between retries in seconds
        backoff_factor: Multiplier for delay after each failure
        jitter: Whether to add random jitter to delay
        max_delay: Maximum delay cap in seconds

    Returns:
        Decorated function with retry logic
    """
    return with_retry(
        max_attempts=max_attempts,
        delay=delay,
        backoff_factor=backoff_factor,
        jitter=jitter,
        exceptions=exception_types,
        max_delay=max_delay,
    )


def network_retry(func: Callable) -> Callable:
    """Smart network retry decorator that extracts retry configuration from function parameters

    This decorator automatically extracts 'retries' and 'interval' parameters from the decorated
    function's arguments and applies dynamic retry logic accordingly. If these parameters are not
    provided, it falls back to sensible defaults.

    Args:
        func: Function to be decorated

    Returns:
        Decorated function with dynamic retry capability

    Example:
        @network_retry
        def http_get(url, retries=3, interval=1.0):
            # Function implementation
            pass

        # Uses custom retry configuration
        http_get("http://example.com", retries=5, interval=2.0)

        # Uses default configuration
        http_get("http://example.com")
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        # Extract retry configuration parameters from function arguments
        retries = kwargs.get("retries", 3)
        interval = kwargs.get("interval", 1.0)

        # Validate and normalize parameters
        try:
            retries = max(1, int(retries)) if retries is not None else 3
            interval = max(0.1, float(interval)) if interval is not None else 1.0
        except (ValueError, TypeError):
            # Fall back to defaults if parameter conversion fails
            retries = 3
            interval = 1.0
            logger.warning(f"Invalid retry parameters for {func.__name__}, using defaults")

        # Create dynamic retry decorator with extracted parameters
        retry_decorator = with_retry(
            max_attempts=retries,
            delay=interval,
            backoff_factor=2.0,
            jitter=True,
            exceptions=(ConnectionError, TimeoutError),
            max_delay=30.0,
        )

        # Apply retry logic and execute function
        return retry_decorator(func)(*args, **kwargs)

    return wrapper


# ============================================================================
# Factory Functions and Convenience Methods
# ============================================================================


def create_retry_policy(policy_type: str = "exponential", **kwargs) -> RetryPolicy:
    """Factory function to create retry policies

    Args:
        policy_type: Type of policy ('fixed', 'exponential', 'jitter', 'none')
        **kwargs: Policy-specific parameters

    Returns:
        RetryPolicy: Configured retry policy instance
    """
    policy_type = policy_type.lower()

    if policy_type == "fixed":
        return FixedRetry(**kwargs)
    elif policy_type == "exponential":
        return ExponentialBackoff(**kwargs)
    elif policy_type == "jitter":
        return JitterBackoff(**kwargs)
    elif policy_type == "none":
        return NoRetry()
    else:
        logger.warning(f"Unknown retry policy type: {policy_type}, using exponential backoff")
        return ExponentialBackoff(**kwargs)


def smart_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter: bool = True,
    network_errors_only: bool = True,
):
    """Smart retry decorator that uses intelligent error classification

    Args:
        max_attempts: Maximum retry attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap
        jitter: Whether to add jitter
        network_errors_only: If True, only retry network-related errors

    Returns:
        Decorator function
    """
    if network_errors_only:
        exceptions = (ConnectionError, TimeoutError)
    else:
        exceptions = (Exception,)

    return with_retry(
        max_attempts=max_attempts,
        delay=base_delay,
        backoff_factor=2.0,
        jitter=jitter,
        exceptions=exceptions,
        max_delay=max_delay,
    )
