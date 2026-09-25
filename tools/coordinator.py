#!/usr/bin/env python3

"""
Resource Manager - Thread-safe Resource Management with Dependency Injection

This module provides a centralized resource manager that eliminates global state
and implements dependency injection pattern for better testability and maintainability.

Key Features:
- Thread-safe resource management without global variables
- Dependency injection pattern for better testability
- Singleton pattern for resource sharing
- Automatic initialization from configuration
- Resource lifecycle management
"""

import threading
from typing import Optional

from config.schemas import Config
from core.types import IAuthProvider

from .agent import Agents
from .credential import Credentials
from .logger import get_logger
from .state import CredentialsExhausted

logger = get_logger("manager")


class ResourceManager(IAuthProvider):
    """Thread-safe resource manager using singleton pattern"""

    _instance: Optional["ResourceManager"] = None
    _lock = threading.Lock()

    def __init__(self, config: Optional[Config] = None):
        """Initialize resource manager with configuration

        Args:
            config: Configuration object, loads from global config if None
        """
        if config is None:
            # Lazy import: config/__init__ imports config.loader at module level,
            # which may import tools.logger; a module-level ``from config import
            # get_config`` here would close that cycle at package init.
            from config import get_config

            config = get_config()

        self._config = config
        self._credentials: Optional[Credentials] = None
        self._agents: Optional[Agents] = None
        self._initialized = False
        self._lock = threading.Lock()

    @classmethod
    def get_instance(cls, config: Optional[Config] = None) -> "ResourceManager":
        """Get singleton instance of resource manager

        Args:
            config: Configuration object for initialization

        Returns:
            ResourceManager: Singleton instance
        """
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(config)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance for testing purposes"""
        with cls._lock:
            cls._instance = None

    def initialize(self) -> None:
        """Initialize resource managers from configuration"""
        with self._lock:
            if self._initialized:
                return

            try:
                # Initialize credentials manager
                github_creds = self._config.global_config.github_credentials
                self._credentials = Credentials(
                    sessions=github_creds.sessions, tokens=github_creds.tokens, strategy=github_creds.strategy.value
                )
                logger.info(
                    f"Initialized credentials manager with {len(github_creds.sessions)} sessions "
                    f"and {len(github_creds.tokens)} tokens"
                )

                # Initialize User-Agent manager
                user_agents = self._config.global_config.user_agents
                self._agents = Agents(user_agents)
                logger.info(f"Initialized User-Agent manager with {len(user_agents)} agents")

                self._initialized = True

            except Exception as e:
                logger.error(f"Failed to initialize resource managers: {e}")
                raise

    def get_credentials(self) -> Credentials:
        """Get credentials manager instance

        Returns:
            Credentials: Credentials manager instance

        Raises:
            RuntimeError: If not initialized
        """
        if not self._initialized or self._credentials is None:
            raise RuntimeError("Resource manager not initialized. Call initialize() first.")
        return self._credentials

    def get_agents(self) -> Agents:
        """Get agents manager instance

        Returns:
            Agents: Agents manager instance

        Raises:
            RuntimeError: If not initialized
        """
        if not self._initialized or self._agents is None:
            raise RuntimeError("Resource manager not initialized. Call initialize() first.")
        return self._agents

    def get_session(self) -> Optional[str]:
        """Get next GitHub session token

        Returns:
            Optional[str]: Session token or None if not available

        Raises:
            CredentialsExhausted: when the bounded selector spent its budget.
                Typed pool exhaustion must reach callers so stages DEFER the
                task; collapsing it to ``None`` would silently complete work
                empty (fix-credential-liveness design D18).
        """
        try:
            return self.get_credentials().get_session()
        except CredentialsExhausted:
            raise
        except Exception as e:
            logger.error(f"Failed to get session: {e}")
            return None

    def get_token(self) -> Optional[str]:
        """Get next GitHub API token

        Returns:
            Optional[str]: API token or None if not available

        Raises:
            CredentialsExhausted: when the bounded selector spent its budget
                (design D18 - never flatten a typed exhaustion into ``None``).
        """
        try:
            return self.get_credentials().get_token()
        except CredentialsExhausted:
            raise
        except Exception as e:
            logger.error(f"Failed to get token: {e}")
            return None

    def get_credential(self, prefer_token: bool = True) -> tuple[str, str]:
        """Get next credential with type preference

        Args:
            prefer_token: Whether to prefer API tokens over sessions

        Returns:
            tuple[str, str]: (credential_value, credential_type)

        Raises:
            RuntimeError: If no credentials available
        """
        return self.get_credentials().get_credential(prefer_token)

    def get_user_agent(self) -> str:
        """Get random User-Agent string

        Returns:
            str: Random User-Agent string
        """
        try:
            return self.get_agents().get()
        except Exception as e:
            logger.error(f"Failed to get User-Agent: {e}")
            # Fallback to default User-Agent
            return "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"


# Backward compatibility functions using singleton pattern
def init_managers() -> None:
    """Initialize resource managers from configuration"""
    manager = ResourceManager.get_instance()
    manager.initialize()


def get_credentials_manager() -> Credentials:
    """Get credentials manager instance

    Returns:
        Credentials: Credentials manager instance

    Raises:
        RuntimeError: If not initialized
    """
    return ResourceManager.get_instance().get_credentials()


def get_agents_manager() -> Agents:
    """Get agents manager instance

    Returns:
        Agents: Agents manager instance

    Raises:
        RuntimeError: If not initialized
    """
    return ResourceManager.get_instance().get_agents()


def get_session() -> Optional[str]:
    """Get next GitHub session token

    Returns:
        Optional[str]: Session token or None if not available
    """
    return ResourceManager.get_instance().get_session()


def get_token() -> Optional[str]:
    """Get next GitHub API token

    Returns:
        Optional[str]: API token or None if not available
    """
    return ResourceManager.get_instance().get_token()


def get_credential(prefer_token: bool = True) -> tuple[str, str]:
    """Get next credential with type preference

    Args:
        prefer_token: Whether to prefer API tokens over sessions

    Returns:
        tuple[str, str]: (credential_value, credential_type)

    Raises:
        RuntimeError: If no credentials available
    """
    return ResourceManager.get_instance().get_credential(prefer_token)


def get_user_agent() -> str:
    """Get random User-Agent string

    Returns:
        str: Random User-Agent string
    """
    return ResourceManager.get_instance().get_user_agent()


def get_managers_stats() -> dict:
    """Get statistics from all resource managers

    Returns:
        dict: Combined statistics from all managers
    """
    stats = {}

    try:
        manager = ResourceManager.get_instance()
        if manager._initialized:
            if manager._credentials:
                stats["credentials"] = manager._credentials.get_stats()
            if manager._agents:
                stats["agents"] = manager._agents.get_stats()
    except Exception as e:
        logger.error(f"Failed to get managers stats: {e}")
        stats["error"] = str(e)

    return stats


def reset_managers_stats() -> None:
    """Reset statistics for all resource managers"""
    try:
        manager = ResourceManager.get_instance()
        if manager._initialized:
            if manager._credentials:
                manager._credentials.reset_stats()
            if manager._agents:
                manager._agents.reset_stats()
            logger.info("Reset statistics for all resource managers")
    except Exception as e:
        logger.error(f"Failed to reset managers stats: {e}")


def update_credentials(sessions: list[str], tokens: list[str]) -> None:
    """Update credentials in the resource manager

    Args:
        sessions: New list of session tokens
        tokens: New list of API tokens
    """
    try:
        manager = ResourceManager.get_instance()
        if manager._initialized and manager._credentials:
            manager._credentials.update_sessions(sessions)
            manager._credentials.update_tokens(tokens)
            logger.info(f"Updated credentials: {len(sessions)} sessions, {len(tokens)} tokens")
    except Exception as e:
        logger.error(f"Failed to update credentials: {e}")


def update_user_agents(user_agents: list[str]) -> None:
    """Update User-Agent strings in the resource manager

    Args:
        user_agents: New list of User-Agent strings
    """
    try:
        manager = ResourceManager.get_instance()
        if manager._initialized and manager._agents:
            manager._agents.update_agents(user_agents)
            logger.info(f"Updated User-Agents: {len(user_agents)} agents")
    except Exception as e:
        logger.error(f"Failed to update User-Agents: {e}")
