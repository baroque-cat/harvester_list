#!/usr/bin/env python3

"""
GitHub Credentials Manager

This module manages GitHub session tokens and API tokens with load balancing.
It provides thread-safe access to multiple credentials for improved concurrency.

Key Features:
- Load balanced credential distribution
- Support for both session tokens and API tokens
- Thread-safe credential access
- Usage statistics and monitoring
- Automatic fallback between credential types
"""

import contextlib
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from constant.system import SERVICE_TYPE_GITHUB_API, SERVICE_TYPE_GITHUB_WEB

from .balancer import Balancer, Strategy
from .logger import get_logger
from .state import (
    CredentialsExhausted,
    bump_credential_metric,
    github_credential_state,
    mark_blocking_mode_active,
    mask_credential,
)

logger = get_logger("manager")


def _default_liveness_settings():
    """Built-in credential-liveness defaults (design D2), used fail-open."""
    from config.schemas import CredentialLivenessConfig

    return CredentialLivenessConfig()


def _resolve_liveness_settings():
    """Read the effective ``CredentialLivenessConfig`` from global config."""
    try:
        from config import get_config

        return get_config().credential_liveness
    except Exception:
        return _default_liveness_settings()


# Probe override (design D19): a capability probe must ask "is a credential
# available NOW", never wait one out.  ``fail_fast_liveness()`` installs a
# zero-budget view for the duration of the probe.
_settings_override: Optional[Any] = None


class _FailFastSettings:
    """Zero-budget, always-bounded view of the effective liveness settings."""

    def __init__(self, base: Any):
        self.wait_mode = "bounded"
        self.max_wait_s = 0.0
        self.early_release = getattr(base, "early_release", True)
        self.emergency_threshold = getattr(base, "emergency_threshold", 3)


def _liveness_settings():
    """Resolve the effective ``CredentialLivenessConfig`` (design D2 seam).

    Module-level on purpose so unit tests can monkeypatch it; it fails open to
    the built-in bounded defaults when no global configuration is wired.
    """
    if _settings_override is not None:
        return _settings_override
    return _resolve_liveness_settings()


@contextlib.contextmanager
def fail_fast_liveness():
    """Force the selector to report exhaustion immediately instead of waiting.

    Used by startup capability probes (design D19 / spec S24: "no startup sleep
    occurs").  Also pins ``wait_mode`` to ``bounded`` so a ``blocking`` rollback
    config can never hang boot.
    """
    global _settings_override
    previous = _settings_override
    _settings_override = _FailFastSettings(_resolve_liveness_settings())
    try:
        yield
    finally:
        _settings_override = previous


@dataclass
class CredentialStats:
    """Credential usage statistics"""

    total_requests: int
    session_requests: int
    token_requests: int
    sessions_count: int
    tokens_count: int
    session_percentage: float
    token_percentage: float
    session_stats: Optional[Dict] = None
    token_stats: Optional[Dict] = None

    @property
    def has_sessions(self) -> bool:
        """Check if sessions are available"""
        return self.sessions_count > 0

    @property
    def has_tokens(self) -> bool:
        """Check if tokens are available"""
        return self.tokens_count > 0

    @property
    def total_credentials(self) -> int:
        """Total number of credentials"""
        return self.sessions_count + self.tokens_count


class Credentials:
    """GitHub credentials manager with load balancing"""

    def __init__(self, sessions: List[str], tokens: List[str], strategy: str = "round_robin"):
        """Initialize credentials manager

        Args:
            sessions: List of GitHub session tokens
            tokens: List of GitHub API tokens
            strategy: Load balancing strategy ("round_robin" or "random")
        """
        if not sessions and not tokens:
            raise ValueError("At least one session or token must be provided")

        self.sessions = sessions.copy() if sessions else []
        self.tokens = tokens.copy() if tokens else []

        # Convert string strategy to enum
        strategy_enum = Strategy.ROUND_ROBIN if strategy == "round_robin" else Strategy.RANDOM

        # Create balancers for each credential type
        self.session_balancer = Balancer(self.sessions, strategy_enum) if self.sessions else None
        self.token_balancer = Balancer(self.tokens, strategy_enum) if self.tokens else None

        self.lock = threading.Lock()
        self.total_requests = 0
        self.session_requests = 0
        self.token_requests = 0

        # Bounded-selector emergency accounting (design D7): consecutive
        # exhaustion episodes per service and whether the current run has
        # already tripped the loud emergency.  A success resets the streak.
        self._exhaustion_streak: Dict[str, int] = {}
        self._emergency_tripped: set = set()

    def get_session(self) -> Optional[str]:
        """Get next session token

        Returns:
            Optional[str]: Session token or None if no sessions available
        """
        credential = self._get_available(
            service=SERVICE_TYPE_GITHUB_WEB,
            balancer=self.session_balancer,
            items=self.sessions,
            label="session",
        )
        if credential:
            with self.lock:
                self.total_requests += 1
                self.session_requests += 1
        return credential

    def get_token(self) -> Optional[str]:
        """Get next API token

        Returns:
            Optional[str]: API token or None if no tokens available
        """
        credential = self._get_available(
            service=SERVICE_TYPE_GITHUB_API,
            balancer=self.token_balancer,
            items=self.tokens,
            label="token",
        )
        if credential:
            with self.lock:
                self.total_requests += 1
                self.token_requests += 1
        return credential

    def _get_available(
        self,
        service: str,
        balancer: Optional[Balancer[str]],
        items: List[str],
        label: str,
    ) -> Optional[str]:
        """Get a credential that is not cooling down.

        ``bounded`` (default): every sleep is bounded by
        ``credential_liveness.max_wait_s`` via a deadline computed before the
        sleep; when the budget is spent the typed :class:`CredentialsExhausted`
        is raised (never a falsy return for a configured pool, never an
        unbounded wait, never a hot spin).  ``blocking`` restores the pre-change
        unbounded waiter byte-for-byte behind a config flip (design D2).
        """
        if not balancer or not items:
            return None

        settings = _liveness_settings()
        if str(getattr(settings, "wait_mode", "bounded")).lower() == "blocking":
            mark_blocking_mode_active()
            return self._get_available_blocking(service, balancer, items, label)

        return self._get_available_bounded(service, balancer, items, label, settings)

    def _get_available_blocking(
        self,
        service: str,
        balancer: Balancer[str],
        items: List[str],
        label: str,
    ) -> Optional[str]:
        """Legacy unbounded waiter (rollback parity, design D2/S6)."""
        while True:
            count = len(items)
            for _ in range(count):
                credential = balancer.get()
                if not github_credential_state.is_cooling(service, credential):
                    self._record_credential_success(service)
                    return credential

            wait = github_credential_state.next_wait(service, items)
            if wait <= 0:
                time.sleep(0.1)
                continue

            masked = ", ".join(mask_credential(item) for item in items)
            logger.warning(
                f"[github] all {label} credentials are cooling down, pause search for {wait:.1f}s, "
                f"credentials: {masked}"
            )
            time.sleep(wait)

    def _get_available_bounded(
        self,
        service: str,
        balancer: Balancer[str],
        items: List[str],
        label: str,
        settings: Any,
    ) -> Optional[str]:
        """Deadline-bounded waiter (design D2): one log line per wait episode."""
        try:
            budget = float(settings.max_wait_s)
        except Exception:
            budget = 60.0
        spent = 0.0
        episode_logged = False

        while True:
            count = len(items)
            for _ in range(count):
                credential = balancer.get()
                if not github_credential_state.is_cooling(service, credential):
                    self._record_credential_success(service)
                    return credential

            wait = github_credential_state.next_wait(service, items)
            if wait is None or wait <= 0:
                # State anomaly: everything is cooling but no positive release
                # time exists.  Fail fast instead of hot-spinning (design D3).
                self._record_exhaustion(service, settings)
                raise CredentialsExhausted(service=service, reason="spin_anomaly", wait_estimate_s=wait)

            remaining = budget - spent
            if remaining <= 0:
                self._record_exhaustion(service, settings)
                raise CredentialsExhausted(service=service, reason="budget_spent", wait_estimate_s=wait)

            sleep_for = min(float(wait), remaining)
            if not episode_logged:
                episode_logged = True
                masked = ", ".join(mask_credential(item) for item in items)
                logger.warning(
                    f"[github] all {label} credentials are cooling down, waiting up to "
                    f"{sleep_for:.1f}s (budget {budget:.1f}s), credentials: {masked}"
                )
            time.sleep(sleep_for)
            spent += sleep_for

    def _record_credential_success(self, service: str) -> None:
        """A usable credential was acquired: reset this service's emergency streak."""
        self._exhaustion_streak.pop(service, None)
        self._emergency_tripped.discard(service)

    def _record_exhaustion(self, service: str, settings: Any) -> None:
        """Count one exhaustion episode and trip the bounded emergency (design D7)."""
        bump_credential_metric("exhausted_episodes")
        try:
            threshold = int(getattr(settings, "emergency_threshold", 3) or 3)
        except Exception:
            threshold = 3
        streak = self._exhaustion_streak.get(service, 0) + 1
        self._exhaustion_streak[service] = streak
        if streak >= threshold and service not in self._emergency_tripped:
            self._emergency_tripped.add(service)
            bump_credential_metric("emergency_trips")
            logger.error(
                f"[github] credential emergency for {service}: {streak} consecutive exhaustion "
                f"episodes (threshold {threshold}); tasks will defer, pipeline keeps running"
            )

    def get_credential(self, prefer_token: bool = True) -> Tuple[str, str]:
        """Get next credential with type preference

        Args:
            prefer_token: Whether to prefer API tokens over sessions

        Returns:
            Tuple[str, str]: (credential_value, credential_type)

        Raises:
            RuntimeError: If no credentials are available
        """
        if prefer_token and self.token_balancer:
            token = self.get_token()
            if token:
                return token, "token"

        if self.session_balancer:
            session = self.get_session()
            if session:
                return session, "session"

        if not prefer_token and self.token_balancer:
            token = self.get_token()
            if token:
                return token, "token"

        raise RuntimeError("No credentials available")

    def get_any(self) -> Tuple[str, str]:
        """Get any available credential

        Returns:
            Tuple[str, str]: (credential_value, credential_type)

        Raises:
            RuntimeError: If no credentials are available
        """
        return self.get_credential(prefer_token=True)

    def has_sessions(self) -> bool:
        """Check if sessions are available

        Returns:
            bool: True if sessions are available
        """
        return bool(self.sessions)

    def has_tokens(self) -> bool:
        """Check if tokens are available

        Returns:
            bool: True if tokens are available
        """
        return bool(self.tokens)

    def has_credentials(self) -> bool:
        """Check if any credentials are available

        Returns:
            bool: True if any credentials are available
        """
        return self.has_sessions() or self.has_tokens()

    def update_sessions(self, sessions: List[str]) -> None:
        """Update session tokens list

        Args:
            sessions: New list of session tokens
        """
        with self.lock:
            self.sessions = sessions.copy() if sessions else []
            if self.sessions:
                if self.session_balancer:
                    self.session_balancer.update_items(self.sessions)
                else:
                    strategy = self.token_balancer.strategy if self.token_balancer else Strategy.ROUND_ROBIN
                    self.session_balancer = Balancer(self.sessions, strategy)
            else:
                self.session_balancer = None

    def update_tokens(self, tokens: List[str]) -> None:
        """Update API tokens list

        Args:
            tokens: New list of API tokens
        """
        with self.lock:
            self.tokens = tokens.copy() if tokens else []
            if self.tokens:
                if self.token_balancer:
                    self.token_balancer.update_items(self.tokens)
                else:
                    strategy = self.session_balancer.strategy if self.session_balancer else Strategy.ROUND_ROBIN
                    self.token_balancer = Balancer(self.tokens, strategy)
            else:
                self.token_balancer = None

    def reset_stats(self) -> None:
        """Reset usage statistics"""
        with self.lock:
            self.total_requests = 0
            self.session_requests = 0
            self.token_requests = 0
            if self.session_balancer:
                self.session_balancer.reset()
            if self.token_balancer:
                self.token_balancer.reset()

    def get_stats(self) -> CredentialStats:
        """Get usage statistics

        Returns:
            CredentialStats: Comprehensive usage statistics
        """
        with self.lock:
            # Calculate percentages
            if self.total_requests > 0:
                session_percentage = round((self.session_requests / self.total_requests) * 100, 2)
                token_percentage = round((self.token_requests / self.total_requests) * 100, 2)
            else:
                session_percentage = 0.0
                token_percentage = 0.0

            return CredentialStats(
                total_requests=self.total_requests,
                session_requests=self.session_requests,
                token_requests=self.token_requests,
                sessions_count=len(self.sessions),
                tokens_count=len(self.tokens),
                session_percentage=session_percentage,
                token_percentage=token_percentage,
                session_stats=self.session_balancer.get_stats() if self.session_balancer else None,
                token_stats=self.token_balancer.get_stats() if self.token_balancer else None,
            )

    def __str__(self) -> str:
        """String representation

        Returns:
            str: String representation
        """
        return f"Credentials(sessions={len(self.sessions)}, tokens={len(self.tokens)}, requests={self.total_requests})"

    def __repr__(self) -> str:
        """Detailed string representation

        Returns:
            str: Detailed representation
        """
        return f"Credentials(sessions={self.sessions}, tokens={self.tokens})"
