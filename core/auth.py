#!/usr/bin/env python3

"""
Core Authentication Services

This module provides a centralized GitHub authentication service that can be configured
by upper layers and used by lower layers without creating circular dependencies.
"""

import threading
from typing import Callable, Optional

from .types import IAuthProvider


def _is_typed_exhaustion(exc: BaseException) -> bool:
    """True for the credential layer's typed pool-exhaustion signal.

    Lazy import on purpose: ``core`` must not depend on ``tools`` at module
    import time (``core/__init__`` -> ``core.auth`` -> ``tools.state`` ->
    ``tools/__init__`` -> ``config`` -> ``core.models`` would close a cycle).
    """
    try:
        from tools.state import CredentialsExhausted
    except Exception:  # pragma: no cover - defensive
        return False
    return isinstance(exc, CredentialsExhausted)


class GithubAuthProvider(IAuthProvider):
    """Centralized GitHub authentication service with dependency injection."""

    _instance: Optional["GithubAuthProvider"] = None
    _lock = threading.Lock()
    _session_provider: Optional[Callable[[], Optional[str]]] = None
    _token_provider: Optional[Callable[[], Optional[str]]] = None
    _user_agent_provider: Optional[Callable[[], str]] = None

    def __init__(self):
        """Initialize with default implementations."""
        pass

    @classmethod
    def get_instance(cls) -> "GithubAuthProvider":
        """Get thread-safe singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance (mainly for testing)."""
        with cls._lock:
            cls._instance = None

    @classmethod
    def configure(
        cls,
        session_provider: Callable[[], Optional[str]],
        token_provider: Callable[[], Optional[str]],
        user_agent_provider: Callable[[], str],
    ):
        """Configure authentication providers.

        Args:
            session_provider: Function to get GitHub session token
            token_provider: Function to get GitHub API token
            user_agent_provider: Function to get User-Agent string
        """
        _ = cls.get_instance()
        cls._session_provider = session_provider
        cls._token_provider = token_provider
        cls._user_agent_provider = user_agent_provider

    def get_session(self) -> Optional[str]:
        """Get GitHub web session token.

        A typed credential exhaustion is re-raised rather than flattened to
        ``None`` (design D18): ``None`` means "no credential configured" and
        callers would silently complete work empty.
        """
        if self.__class__._session_provider:
            try:
                return self.__class__._session_provider()
            except Exception as exc:
                if _is_typed_exhaustion(exc):
                    raise
                return None
        return None

    def get_token(self) -> Optional[str]:
        """Get GitHub API token.

        A typed credential exhaustion is re-raised rather than flattened to
        ``None`` (design D18).
        """
        if self.__class__._token_provider:
            try:
                return self.__class__._token_provider()
            except Exception as exc:
                if _is_typed_exhaustion(exc):
                    raise
                return None
        return None

    def get_credential(self, prefer_token: bool = True) -> tuple[str, str]:
        """Get next credential with type preference.

        Args:
            prefer_token: Whether to prefer API tokens over sessions

        Returns:
            tuple[str, str]: (credential_value, credential_type)

        Raises:
            RuntimeError: If no credentials available
        """
        if prefer_token:
            token = self.get_token()
            if token:
                return (token, "token")
            session = self.get_session()
            if session:
                return (session, "session")
        else:
            session = self.get_session()
            if session:
                return (session, "session")
            token = self.get_token()
            if token:
                return (token, "token")

        raise RuntimeError("No credentials available")

    def get_user_agent(self) -> str:
        """Get User-Agent string."""
        if self.__class__._user_agent_provider:
            try:
                return self.__class__._user_agent_provider()
            except Exception:
                pass
        return "DefaultUserAgent/1.0"


# Convenience functions for easy access
def get_auth_provider() -> IAuthProvider:
    """Get the core auth provider instance."""
    return GithubAuthProvider.get_instance()


def configure_auth(
    session_provider: Callable[[], Optional[str]],
    token_provider: Callable[[], Optional[str]],
    user_agent_provider: Callable[[], str],
):
    """Configure the global authentication service.

    This should be called by the manager layer during initialization.
    """
    GithubAuthProvider.configure(session_provider, token_provider, user_agent_provider)
