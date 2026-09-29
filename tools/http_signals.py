#!/usr/bin/env python3

"""Shared wire-signal recognition for outbound HTTP refusals.

One recognition rule, shared by every surface that reads a refusal off the wire
(the gather transport and the LLM-provider transport), so the same response can
never yield two different verdicts (provider-refusal-taxonomy, design D1).  This
is why there are three functions and not four copies: a 403 carrying rate-limit
text means "slow down" on both surfaces, or on neither.

Dependency-light on purpose: stdlib only, no import of ``search.client`` and no
provider knowledge.  Dialect-independent by design (D11): only status 429, a 403
carrying the proven limit markers, and a wait that was actually published are
recognised here.  Any provider-specific rule must arrive with a captured fixture.
"""

import json
import re
import time
from email.utils import parsedate_to_datetime
from typing import Dict, Optional

# The marker vocabulary proven on the GitHub surface, reused verbatim so the two
# surfaces agree (design D1/D11).
_LIMIT_MARKERS = re.compile(r"rate limit|abuse detection|secondary rate limit", re.I)

# Human-readable wait embedded in a refusal body (fallback only).
_WAIT_IN_CONTENT = re.compile(
    r"(?:retry after|try again in|wait)\s+(\d+)\s*(second|minute|hour)s?", flags=re.I
)

# Conservative fallback for the "few minutes" phrasing (matches the pre-change
# copies byte-for-byte).
_FEW_MINUTES_WAIT_S = 180.0


def is_capacity_refusal(status: int, text: str) -> bool:
    """True when ``(status, text)`` carries a throttling (capacity) signal.

    A 429 is always a capacity refusal; a 403 only qualifies when its body or
    headers carry a rate-limit / abuse marker.  A bare 403 (or 401) is an
    authentication failure, not capacity (PRT-S2/PRT-S3).
    """
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    if code == 429:
        return True
    if code == 403 and bool(_LIMIT_MARKERS.search(text or "")):
        return True
    return False


def _normalized(headers: Dict[str, str]) -> Dict[str, str]:
    return {str(key).lower(): str(value) for key, value in (headers or {}).items()}


def wait_from_headers(headers: Dict[str, str]) -> Optional[float]:
    """Read a published finite wait from ``Retry-After`` / ``X-RateLimit-Reset``.

    ``Retry-After`` may be a delta-seconds integer or an HTTP-date; the reset
    header is treated as an absolute epoch timestamp.  Returns ``None`` when the
    remote published no usable resumption time.
    """
    normalized = _normalized(headers)
    retry_after = str(normalized.get("retry-after", "")).strip()
    if retry_after:
        if retry_after.isdigit():
            return float(retry_after)
        try:
            return max(0.0, parsedate_to_datetime(retry_after).timestamp() - time.time())
        except Exception:
            pass

    reset_at = str(normalized.get("x-ratelimit-reset", "")).strip()
    if reset_at.isdigit():
        return max(0.0, float(reset_at) - time.time())

    return None


def wait_from_content(content: str) -> Optional[float]:
    """Parse a human-readable wait out of a refusal body (fallback only)."""
    text = content or ""
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            text = str(data.get("message", text))
    except Exception:
        pass

    match = _WAIT_IN_CONTENT.search(text)
    if match:
        value = float(match.group(1))
        unit = match.group(2).lower()
        if unit.startswith("hour"):
            return value * 3600
        if unit.startswith("minute"):
            return value * 60
        return value

    if re.search(r"few minutes", text, flags=re.I):
        return _FEW_MINUTES_WAIT_S

    return None
