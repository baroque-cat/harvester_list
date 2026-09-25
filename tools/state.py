#!/usr/bin/env python3

"""
GitHub credential cooldown and masking helpers.
"""

import hashlib
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from constant.system import (
    GITHUB_CREDENTIAL_COOLDOWN_FACTOR,
    GITHUB_CREDENTIAL_COOLDOWN_MAX,
    GITHUB_CREDENTIAL_COOLDOWN_MIN,
)


from .logger import get_logger

logger = get_logger("state")


def mask_credential(credential: str) -> str:
    """Mask a credential while keeping it recognizable"""
    text = credential or ""
    if len(text) <= 4:
        return "*" * len(text)
    if len(text) <= 12:
        return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"
    return f"{text[:6]}{'*' * max(6, len(text) - 10)}{text[-4:]}"


def credential_bucket_key(service: str, credential: str) -> str:
    """Build a stable non-sensitive rate-limit bucket key"""
    digest = hashlib.sha256((credential or "").encode("utf-8")).hexdigest()[:12]
    return f"{service}:{digest}" if service and credential else service


class GithubCredentialLimited(Exception):
    """Raised when a single GitHub credential is cooling down"""

    def __init__(self, service: str, credential: str, wait: float, reason: str = ""):
        self.service = service
        self.credential = credential
        self.wait = wait
        self.reason = reason
        super().__init__(f"{service} credential is rate limited for {wait:.1f}s")


class CredentialsExhausted(Exception):
    """Raised when the bounded credential selector spends its budget.

    Sibling of :class:`GithubCredentialLimited` rather than a subclass: this is
    a pool-level, deferred outcome (the stage routes it to ``RateLimitDeferral``),
    not a per-credential rotation signal.  ``reason`` is one of ``budget_spent``
    (the configured wait budget was consumed), ``spin_anomaly`` (the earliest
    release probe was non-positive/undeterminable), or ``all_cooling`` (pool
    refused before any wait).  ``wait_estimate_s`` is the best-known time until
    the earliest release, or ``None`` when unknown (design D1).
    """

    def __init__(
        self,
        service: str,
        reason: str,
        wait_estimate_s: Optional[float] = None,
        episode_id: Optional[str] = None,
    ):
        self.service = service
        self.reason = reason
        self.wait_estimate_s = wait_estimate_s
        self.episode_id = episode_id
        super().__init__(f"{service} credentials exhausted ({reason})")


# ---------------------------------------------------------------------------
# Credential-liveness observability (design D10)
# ---------------------------------------------------------------------------
_credential_metrics_lock = threading.Lock()
_credential_metrics: Dict[str, int] = {
    "exhausted_episodes": 0,
    "deferred_by_credentials": 0,
    "secondary_limit_incidents": 0,
    "early_releases": 0,
    "emergency_trips": 0,
    "blocking_mode_active": 0,
}


def credential_liveness_metrics() -> Dict[str, int]:
    """Thread-safe snapshot of the credential-liveness counters."""
    with _credential_metrics_lock:
        return dict(_credential_metrics)


def credential_metrics_summary() -> str:
    """One-line rendering of the credential-liveness counters (design D20).

    ``PipelineStatus.credential_metrics`` is the programmatic surface required by
    the spec; this renders the same snapshot for operators and for the live-gate
    acceptance rows (A9-A13), which must be evidenceable from a real run.
    Key order follows the collector's declaration order.
    """
    with _credential_metrics_lock:
        snapshot = dict(_credential_metrics)
    return ", ".join(f"{key}={snapshot[key]}" for key in snapshot)


def bump_credential_metric(name: str, amount: int = 1) -> None:
    """Increment a credential-liveness counter (creating it if absent)."""
    with _credential_metrics_lock:
        _credential_metrics[name] = _credential_metrics.get(name, 0) + int(amount)


def mark_blocking_mode_active() -> None:
    """Flag that the legacy unbounded (blocking) selector mode ran at least once."""
    with _credential_metrics_lock:
        _credential_metrics["blocking_mode_active"] = 1


def reset_credential_liveness_metrics() -> None:
    """Test/run helper: clear every counter back to its zero value."""
    with _credential_metrics_lock:
        for key in _credential_metrics:
            _credential_metrics[key] = 0


def _early_release_enabled() -> bool:
    """Resolve ``credential_liveness.early_release`` (fail-open on missing config)."""
    try:
        from config import get_config

        return bool(get_config().credential_liveness.early_release)
    except Exception:
        return True


@dataclass
class Cooldown:
    until: float = 0.0
    failures: int = 0
    last_wait: float = 0.0
    kind: str = "primary"


class GithubCredentialState:
    """Thread-safe cooldown registry for GitHub credentials"""

    def __init__(self) -> None:
        self._items: Dict[Tuple[str, str], Cooldown] = {}
        self._lock = threading.Lock()

    def mark_limited(
        self, service: str, credential: str, wait: Optional[float] = None, kind: str = "primary"
    ) -> float:
        """Mark a credential as cooling down and return the selected wait.

        ``kind`` records whether the limit is a token-scoped primary quota event
        (``primary``) or a subject-scoped secondary/abuse limit (``secondary``);
        the escalation schedule is identical for both (design D5).
        """
        if not service or not credential:
            return 0.0

        now = time.time()
        key = (service, credential)

        with self._lock:
            item = self._items.get(key, Cooldown())
            if wait is None or wait <= 0:
                wait = self._next_backoff(item)
            wait = self._clamp(wait)
            item.failures += 1
            item.last_wait = wait
            item.kind = kind
            item.until = max(item.until, now + wait)
            self._items[key] = item
            return wait

    def mark_success(self, service: str, credential: str) -> None:
        """Clear cooldown state after a successful request.

        With ``credential_liveness.early_release`` enabled (default) a success is
        direct evidence the credential works, so the entry is dropped immediately
        and the early-release counter is bumped (design D4).  When disabled, the
        expired-only pop is preserved byte-for-byte (config-flip rollback).
        """
        if not service or not credential:
            return

        now = time.time()
        with self._lock:
            key = (service, credential)
            item = self._items.get(key)
            if item is None:
                return
            if _early_release_enabled():
                released_early = item.until > now
                self._items.pop(key, None)
                if released_early:
                    bump_credential_metric("early_releases")
                    logger.info(
                        f"[github] credential {mask_credential(credential)} released early "
                        f"after a successful request ({service})"
                    )
            elif item.until <= now:
                self._items.pop(key, None)

    def is_cooling(self, service: str, credential: str) -> bool:
        """Return whether a credential is still cooling down"""
        return self.wait_time(service, credential) > 0

    def wait_time(self, service: str, credential: str) -> float:
        """Return remaining cooldown seconds for a credential"""
        if not service or not credential:
            return 0.0

        now = time.time()
        key = (service, credential)

        with self._lock:
            item = self._items.get(key)
            if not item:
                return 0.0
            wait = item.until - now
            if wait <= 0:
                return 0.0
            return wait

    def all_cooling(self, service: str, credentials: List[str]) -> bool:
        """Return whether all credentials are cooling down"""
        items = [x for x in credentials if x]
        return bool(items) and all(self.is_cooling(service, item) for item in items)

    def next_wait(self, service: str, credentials: List[str]) -> float:
        """Return the earliest cooldown release among credentials"""
        waits = [self.wait_time(service, item) for item in credentials if item]
        waits = [wait for wait in waits if wait > 0]
        return min(waits) if waits else 0.0

    def _next_backoff(self, item: Cooldown) -> float:
        if item.failures <= 0 or item.last_wait <= 0:
            return float(GITHUB_CREDENTIAL_COOLDOWN_MIN)
        return item.last_wait * GITHUB_CREDENTIAL_COOLDOWN_FACTOR

    def _clamp(self, wait: float) -> float:
        return min(float(GITHUB_CREDENTIAL_COOLDOWN_MAX), max(float(GITHUB_CREDENTIAL_COOLDOWN_MIN), float(wait)))


github_credential_state = GithubCredentialState()
