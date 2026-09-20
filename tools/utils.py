#!/usr/bin/env python3

"""
Utility functions for the search engine.
"""

import functools
import re
import traceback
from typing import Any, Callable, TypeVar

from constant.system import PROVIDER_SERVICE_PREFIX

from .logger import get_logger

logger = get_logger("tools")
F = TypeVar("F", bound=Callable[..., Any])


def handle_exceptions(
    default_result: Any = None,
    log_level: str = "error",
    reraise: bool = False,
    exception_types: tuple = (Exception,),
    exclude: tuple = (),
) -> Callable[[F], F]:
    """Decorator for consistent exception handling.

    Args:
        default_result: Value to return on exception
        log_level: Logging level (debug, info, warning, error, critical)
        reraise: Whether to reraise the exception after logging
        exception_types: Tuple of exception types to catch
        exclude: Exception types that must propagate even though they match
            ``exception_types`` (used to narrow a catch-all fail-open contract)

    Returns:
        Decorated function with exception handling
    """

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except exclude:
                raise
            except exception_types as e:
                # Extract context information
                context = {
                    "function": func.__name__,
                    "module": func.__module__,
                    "args_count": len(args),
                }

                # Log the exception
                log_message = f"Exception in {func.__name__}: {str(e)}"
                log_func = getattr(logger, log_level, logger.error)
                log_func(f"{log_message} | Context: {context}")

                # Log traceback for debugging
                if log_level in ("error", "critical"):
                    logger.debug(f"Traceback for {func.__name__}:\n{traceback.format_exc()}")

                if reraise:
                    raise

                return default_result

        return wrapper

    return decorator


def trim(text: str) -> str:
    """Trim whitespace from text, return empty string if invalid."""
    if not text or type(text) != str:
        return ""
    return text.strip()


def isblank(text: str) -> bool:
    """Check if text is blank or invalid."""
    return not text or type(text) != str or not text.strip()


def encoding_url(url: str) -> str:
    """Encode Chinese characters in URL to punycode."""
    if not url:
        return ""

    url = url.strip()
    cn_chars = re.findall("[\u4e00-\u9fa5]+", url)
    if not cn_chars:
        return url

    punycodes = list(map(lambda x: "xn--" + x.encode("punycode").decode("utf-8"), cn_chars))
    for c, pc in zip(cn_chars, punycodes):
        url = url[: url.find(c)] + pc + url[url.find(c) + len(c) :]

    return url


def get_service_name(provider: str) -> str:
    """Get service name for rate limiting

    Args:
        provider: Provider name to process

    Returns:
        str: Processed service name for rate limiting
    """
    name = trim(provider)
    if not name:
        return ""

    return f"{PROVIDER_SERVICE_PREFIX}:{name}"
