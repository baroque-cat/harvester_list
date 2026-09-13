#!/usr/bin/env python3

"""
Shared Regex Patterns - Centralized regex compilation for performance
"""

import re
from typing import List, Pattern

# API key patterns for redaction
API_KEY_PATTERNS = [
    r"\bAIza[0-9A-Za-z_-]{35}",  # Google API keys (Gemini)
    r"\bsk-[0-9A-Za-z_-]{20,}",  # OpenAI and sk- prefixed keys
    r"\bsk-proj-[0-9A-Za-z_-]{20,}",  # OpenAI project keys
    r"\banthrop[0-9A-Za-z_-]{20,}",  # Anthropic keys
    r"\bgsk_[0-9A-Za-z_-]{20,}",  # GooeyAI keys
    r"\bstab_[0-9A-Za-z_-]{20,}",  # StabilityAI keys
    r"\bghp_[A-Za-z0-9]{36}",  # GitHub personal access tokens (classic)
    r"\bgithub_pat_[A-Za-z0-9_]{20,}",  # GitHub fine-grained PATs
    r"\bgho_[A-Za-z0-9]{36}",  # GitHub OAuth tokens
]

# Query parsing patterns
GITHUB_QUERY_PATTERN = r"/([^/]+)/"

# Pre-compiled patterns for performance
COMPILED_API_KEY_PATTERNS: List[Pattern[str]] = [re.compile(pattern) for pattern in API_KEY_PATTERNS]

COMPILED_GITHUB_QUERY_PATTERN: Pattern[str] = re.compile(GITHUB_QUERY_PATTERN)


def redact_api_key(key: str) -> str:
    """Redact API key for safe logging (show first 6 and last 6 characters)"""
    if len(key) <= 12:
        return "*" * len(key)
    return f"{key[:6]}...{key[-6:]}"


def redact_api_keys_in_text(text: str) -> str:
    """Redact all API keys found in text"""
    result = text
    for pattern in COMPILED_API_KEY_PATTERNS:
        result = pattern.sub(lambda m: redact_api_key(m.group(0)), result)
    return result


def extract_github_query_pattern(query: str) -> str:
    """Extract regex pattern from GitHub search query format"""
    match = COMPILED_GITHUB_QUERY_PATTERN.search(query)
    return match.group(1) if match else ""
