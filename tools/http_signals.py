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
# surfaces agree (design D1/D11).  PUBLIC: this is the one definition, consumed
# by the refusal classifier here and by the credential-cooling content detector in
# ``search.client``.  A fourth private copy of these three phrases is how the
# original defect happened, so a second copy is a test failure (PRT-S16).
LIMIT_MARKERS = re.compile(r"rate limit|abuse detection|secondary rate limit", re.I)

# Kept as a private alias so the module's own references and the pins written
# against the predecessor change keep working.
_LIMIT_MARKERS = LIMIT_MARKERS

# Quota / billing exhaustion, transcribed from the markers the shipped providers
# already recognise on the ``check`` surface (design D3 of
# fix-provider-refusal-counters):
#   provider/openai_like.py:123 (403) and :127 (429)
#   provider/anthropic.py:151
#   provider/gemini.py:72 (the specific phrase only)
# Deliberately EXCLUDED, because they are safe only inside a status-scoped
# provider branch and would relabel an authentication refusal at transport level:
#   bare "quota" / "billing" (provider/vertex.py:158)
#   bare "Billing" / "purchase" (provider/anthropic.py:151)
#   "RESOURCE_EXHAUSTED" (provider/gemini.py:72) - Google's code for rate limit
#   AND quota; a 429 carrying it is already a capacity refusal, so importing it
#   here would steal rate-limit outcomes into the quota class.
QUOTA_MARKERS = re.compile(
    r"exceeded_current_quota_error"
    r"|insufficient_user_quota"
    r"|insufficient_quota"
    r"|billing_not_active"
    r"|credit balance is too low"
    r"|quota exceeded for quota metric"
    r"|(?:额度|余额)(?:不足|过低)"
    r"|欠费"
    r"|请充值"
    r"|recharge",
    re.I,
)

# Human-readable wait embedded in a refusal body (fallback only).
_WAIT_IN_CONTENT = re.compile(
    r"(?:retry after|try again in|wait)\s+(\d+)\s*(second|minute|hour)s?", flags=re.I
)

# Conservative fallback for the "few minutes" phrasing (matches the pre-change
# copies byte-for-byte).
_FEW_MINUTES_WAIT_S = 180.0


def is_quota_refusal(text: str) -> bool:
    """True when ``text`` carries a recognised quota / billing-exhaustion marker.

    Opportunistic and status-independent: the marker either is present or it is
    not, and when it is absent the caller falls through to the classification it
    already had.  Fail-open by contract - a classifier must never raise into a
    worker loop - and it never changes an exception type on its own (design D1 of
    ``fix-provider-refusal-counters``: counting is observational).
    """
    try:
        if not isinstance(text, str) or not text:
            return False
        return bool(QUOTA_MARKERS.search(text))
    except Exception:
        return False


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
