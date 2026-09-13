#!/usr/bin/env python3

"""
Status Manager - Unified scheduling entry point for status monitoring

This module provides the single entry point for all status monitoring and display operations.
Inherits PeriodicTaskManager to serve as the new scheduling entry point for the monitoring system.
"""

from typing import Optional

from constant.monitoring import DisplayConfig
from state.collector import StatusCollector
from state.display import StatusDisplayEngine, get_display_config
from state.enums import DisplayMode, StatusContext
from state.models import SystemStatus
from state.types import TaskDataProvider
from tools.logger import get_logger
from tools.utils import handle_exceptions

from .base import PeriodicTaskManager

logger = get_logger("manager")


class StatusManager(PeriodicTaskManager):
    """
    Unified Status Manager - Scheduling entry point for all status operations

    This class serves as the new monitoring entry point, replacing scattered display methods:
    - monitoring.print_status()
    - application.print_status()
    - task_manager._print_progress()
    - main.print_stats()
    - StatusDisplayService.display_status()

    Inherits PeriodicTaskManager to provide scheduling capabilities.

    Usage Examples:
        # Standard system status
        status_manager.show_status()

        # Compact monitoring status
        status_manager.show_status(StatusContext.MONITORING, DisplayMode.COMPACT)

        # Detailed application status
        status_manager.show_status(StatusContext.APPLICATION, DisplayMode.DETAILED)

        # Task manager progress
        status_manager.show_status(StatusContext.TASK_MANAGER, DisplayMode.COMPACT)
    """

    def __init__(
        self,
        collector: StatusCollector,
        task_provider: Optional[TaskDataProvider] = None,
        display_interval: float = 5.0,
    ):
        """Initialize with StatusCollector and optional task provider

        Args:
            collector: StatusCollector instance for data aggregation
            task_provider: Optional task data provider for direct task stats access
            display_interval: Display update interval in seconds
        """
        # Initialize base class with scheduling
        super().__init__("StatusManager", display_interval)

        # Core dependencies
        self.collector = collector
        self.task_provider = task_provider
        self.display_engine = StatusDisplayEngine()

        # Cache for performance
        self._last_status = None
        self._last_update = 0.0

        logger.debug("StatusManager initialized as scheduling entry point")

    def _execute_periodic_task(self) -> None:
        """Execute periodic status display - main scheduling entry point"""
        try:
            # Ingest fresh task data into monitoring, then display it
            status = self._ingest_fresh_task_data()

            # Display status with forced refresh
            self.show_status(refresh=True, status=status)

        except Exception as e:
            logger.error(f"Error in periodic status display: {e}")

    def _ingest_fresh_task_data(self) -> Optional[SystemStatus]:
        """Pull fresh task stats into monitoring (best-effort).

        Returns the freshly built SystemStatus, or None when no task provider
        is available. Keeps monitoring's pipeline snapshot (skip/date metrics
        included) current for any subsequent cached read.
        """
        try:
            if self.task_provider and self.collector.monitoring:
                status = self.task_provider.stats()
                self.collector.monitoring.ingest(status)
                return status
        except Exception as e:
            logger.debug(f"Fresh task stats ingest failed: {e}")
        return None

    def show_status(
        self,
        context: StatusContext = StatusContext.SYSTEM,
        mode: DisplayMode = DisplayMode.STANDARD,
        refresh: bool = False,
        status: SystemStatus = None,
        **options,
    ) -> None:
        """
        Single entry point for all status display operations

        Args:
            context: Display context (system, monitoring, application, etc.)
            mode: Display mode (compact, standard, detailed, etc.)
            refresh: Force data refresh (bypass cache)
            **options: Additional display options
        """
        try:
            # 1. Collect system status
            try:
                if not status or not isinstance(status, SystemStatus):
                    if refresh:
                        # Pull fresh task stats into monitoring first so the
                        # refreshed snapshot carries current pipeline metrics
                        # (skip/date counters included).
                        self._ingest_fresh_task_data()
                    status = self.collector.status(refresh=refresh)
            except Exception as e:
                logger.error(f"Error collecting status: {e}")
                raise

            # 2. Get display configuration
            try:
                display_config = self._get_display_config(context, mode, **options)
            except Exception as e:
                logger.error(f"Error getting display config: {e}")
                raise

            # 3. Render status
            try:
                self.display_engine.render(status, context, mode, display_config)
            except Exception as e:
                logger.error(f"Error rendering status: {e}")
                raise

            # 4. Cache for future use
            self._last_status = status
            self._last_update = status.timestamp

        except Exception as e:
            logger.error(f"Status display failed: {e}")
            self._show_emergency_status(context, mode)

    @handle_exceptions(default_result=False, log_level="error")
    def is_system_healthy(self) -> bool:
        """Check if system is in healthy state"""
        status = self.collector.status(refresh=False)
        return status.healthy()

    @handle_exceptions(default_result=[], log_level="error")
    def get_critical_alerts(self) -> list:
        """Get list of critical alerts"""
        status = self.collector.status(refresh=False)
        return status.critical_alerts()

    def _on_task_completion(self) -> None:
        """Handle task completion event from task provider"""
        logger.info("Received task completion event, marking StatusManager as finished")
        self.mark_finished()

    def clear_cache(self) -> None:
        """Clear all cached data"""
        self.collector.clear_cache()
        self._last_status = None
        self._last_update = 0.0
        logger.debug("StatusManager cache cleared")

    def _get_display_config(self, context: StatusContext, mode: DisplayMode, **options):
        """Get display configuration for the given context and mode"""
        try:
            return get_display_config(context, mode, **options)
        except Exception as e:
            logger.debug(f"Error creating display config: {e}")
            # Return default config with fallback values
            return get_display_config(context, mode, **options)

    def _show_emergency_status(self, context: StatusContext, mode: DisplayMode) -> None:
        """Show emergency status when main display fails"""
        try:
            lines = [
                DisplayConfig.SEPARATOR_MAIN,
                DisplayConfig.TITLE_CENTER_FORMAT.format(
                    title=DisplayConfig.EMERGENCY_TITLE, width=DisplayConfig.DEFAULT_WIDTH
                ),
                DisplayConfig.SEPARATOR_MAIN,
                f"Context: {context.value}",
                f"Mode: {mode.value}",
                DisplayConfig.EMERGENCY_ERROR_MSG,
                DisplayConfig.EMERGENCY_INFO_HEADER,
            ]

            # Try to get basic info from collector
            if self.collector.monitoring:
                lines.append(DisplayConfig.MONITORING_AVAILABLE)
            else:
                lines.append(DisplayConfig.MONITORING_NOT_AVAILABLE)

            if self.task_provider:
                lines.append(DisplayConfig.TASK_PROVIDER_AVAILABLE)
            else:
                lines.append(DisplayConfig.TASK_PROVIDER_NOT_AVAILABLE)

            lines.append(DisplayConfig.SEPARATOR_MAIN)

            for line in lines:
                logger.info(line)

        except Exception as e:
            logger.error(f"Emergency status display also failed: {e}")
            logger.info(DisplayConfig.SEPARATOR_EMERGENCY)
            logger.info(DisplayConfig.CRITICAL_FAILURE_MSG)
            logger.info(DisplayConfig.SEPARATOR_EMERGENCY)
