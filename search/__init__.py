#!/usr/bin/env python3

"""
Advanced search engine with adaptive query refinement for GitHub code search.
"""

# Explicit re-exports for stable public API
from .client import (
    chat,
    collect,
    estimate_web_total,
    extract,
    get_github_client,
    get_github_stats,
    http_get,
    init_github_client,
    log_github_stats,
    search_api_with_count,
    search_code,
    search_github_api,
    search_github_web,
    search_web_with_count,
)

__all__ = [
    "chat",
    "collect",
    "estimate_web_total",
    "extract",
    "get_github_client",
    "get_github_stats",
    "http_get",
    "init_github_client",
    "log_github_stats",
    "search_api_with_count",
    "search_code",
    "search_github_api",
    "search_github_web",
    "search_web_with_count",
]
