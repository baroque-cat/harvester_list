#!/usr/bin/env python3

"""
Configuration Validator

This module provides comprehensive validation for configuration objects.
It ensures configuration completeness, correctness, and consistency.

Key Features:
- Type validation
- Business rule validation
- Dependency checking
- Error reporting
"""

from typing import List

from .schemas import Config, LoadBalanceStrategy, TaskConfig


class ConfigValidator:
    """Configuration validator with comprehensive checks"""

    def __init__(self):
        """Initialize configuration validator"""
        self.errors: List[str] = []
        self.warnings: List[str] = []

    def validate(self, config: Config) -> None:
        """Validate complete configuration

        Args:
            config: Configuration object to validate

        Raises:
            ValueError: If validation fails
        """
        self.errors.clear()
        self.warnings.clear()

        # Validate global configuration
        self._validate_global_config(config)

        # Validate pipeline configuration
        self._validate_pipeline_config(config)

        # Validate monitoring configuration
        self._validate_monitoring_config(config)

        # Validate tasks configuration
        self._validate_tasks_config(config)

        # Validate worker manager configuration
        self._validate_worker_manager_config(config)

        # Validate registry configuration
        self._validate_registry_config(config)

        # Validate gather-skip configuration
        self._validate_skip_config(config)

        # Validate repository metadata enrichment configuration
        self._validate_enrichment_config(config)

        # Validate frontier-based early-stop configuration
        self._validate_early_stop_config(config)

        # Validate rate limits
        self._validate_rate_limits(config)

        # Validate display configuration
        self._validate_display_config(config)

        # Surface non-fatal configuration warnings.
        # Lazy import: config/* must not import tools at module level —
        # tools.coordinator imports config back (circular at package init).
        if self.warnings:
            from tools.logger import get_logger

            logger = get_logger("config")
            for warning in self.warnings:
                logger.warning(f"Configuration warning: {warning}")

        # Check for validation errors
        if self.errors:
            error_msg = "Configuration validation failed:\n" + "\n".join(f"- {error}" for error in self.errors)
            raise ValueError(error_msg)

    def _validate_global_config(self, config: Config) -> None:
        """Validate global configuration section

        Args:
            config: Configuration object
        """
        global_config = config.global_config

        # Validate workspace
        if not global_config.workspace:
            self.errors.append("Global workspace cannot be empty")

        # Validate GitHub credentials
        credentials = global_config.github_credentials
        if not credentials.sessions and not credentials.tokens:
            self.errors.append("At least one GitHub session or token must be provided")

        # Validate load balance strategy
        if credentials.strategy not in LoadBalanceStrategy:
            self.errors.append(f"Invalid load balance strategy: {credentials.strategy}")

        # Validate user agents
        if not global_config.user_agents:
            self.errors.append("At least one user agent must be provided")

        # Validate max retries
        if global_config.max_retries_requeued < 0:
            self.errors.append("Max retries requeued must be non-negative")

    def _validate_pipeline_config(self, config: Config) -> None:
        """Validate pipeline configuration section

        Args:
            config: Configuration object
        """
        pipeline = config.pipeline

        required_stages = {"search", "gather", "check", "inspect"}
        for stage in required_stages:
            # Validate thread counts
            if stage not in pipeline.threads:
                self.errors.append(f"Missing thread count for stage: {stage}")
            elif pipeline.threads[stage] <= 0:
                self.errors.append(f"Thread count for {stage} must be positive")

            # Validate queue sizes
            if stage not in pipeline.queue_sizes:
                self.errors.append(f"Missing queue size for stage: {stage}")
            elif pipeline.queue_sizes[stage] <= 0:
                self.errors.append(f"Queue size for {stage} must be positive")

    def _validate_monitoring_config(self, config: Config) -> None:
        """Validate monitoring configuration section

        Args:
            config: Configuration object
        """
        monitoring = config.monitoring

        if monitoring.update_interval <= 0:
            self.errors.append("Monitoring update interval must be positive")

        if not (0 <= monitoring.error_threshold <= 1):
            self.errors.append("Error threshold must be between 0 and 1")

        if monitoring.queue_threshold < 0:
            self.errors.append("Queue threshold must be non-negative")

        if monitoring.memory_threshold <= 0:
            self.errors.append("Memory threshold must be positive")

        if monitoring.response_threshold <= 0:
            self.errors.append("Response threshold must be positive")

    def _validate_tasks_config(self, config: Config) -> None:
        """Validate tasks configuration section

        Args:
            config: Configuration object
        """
        if not config.tasks:
            self.errors.append("At least one task must be configured")

        enabled_tasks = [task for task in config.tasks if task.enabled]
        if not enabled_tasks:
            self.errors.append("At least one task must be enabled")

        # Validate task names are unique
        task_names = [task.name for task in config.tasks if task.enabled]
        if len(task_names) != len(set(task_names)):
            self.errors.append("Task names must be unique")

        # Validate individual tasks
        for task in config.tasks:
            self._validate_task(task)

    def _validate_task(self, task: TaskConfig) -> None:
        """Validate individual task configuration

        Args:
            task: Task configuration object
        """
        if not task.enabled:
            return

        if not task.name:
            self.errors.append("Task name cannot be empty")

        if not task.provider_type:
            self.errors.append(f"Provider type cannot be empty for task: {task.name}")

        if task.storage.plan and not task.storage.directory:
            self.errors.append(f"Task {task.name} storage.plan requires storage.directory")

        # Validate stage dependencies
        try:
            task.stages.validate()
        except ValueError as e:
            self.errors.append(f"Task {task.name} stage validation failed: {e}")

        # Validate conditions
        if not task.conditions:
            self.errors.append(f"At least one condition required for task: {task.name}")
        else:
            # Check each condition has valid patterns
            for i, condition in enumerate(task.conditions):
                if not condition.patterns.key_pattern:
                    self.errors.append(f"Key pattern required for condition {i+1} in task: {task.name}")

                if not condition.query and not condition.patterns.key_pattern:
                    self.errors.append(f"Either query or key_pattern required for condition {i+1} in task: {task.name}")

    def _validate_worker_manager_config(self, config: Config) -> None:
        """Validate worker manager configuration

        Args:
            config: Configuration object
        """
        worker_manager = config.worker

        if worker_manager.min_workers < 1:
            self.errors.append("Worker manager min_workers must be at least 1")

        if worker_manager.max_workers < worker_manager.min_workers:
            self.errors.append("Worker manager max_workers must be >= min_workers")

        if worker_manager.target_queue_size < 0:
            self.errors.append("Worker manager target_queue_size must be non-negative")

        if worker_manager.adjustment_interval <= 0:
            self.errors.append("Worker manager adjustment_interval must be positive")

        if not (0 < worker_manager.scale_up_threshold < 1):
            self.errors.append("Worker manager scale_up_threshold must be between 0 and 1")

        if not (0 < worker_manager.scale_down_threshold < 1):
            self.errors.append("Worker manager scale_down_threshold must be between 0 and 1")

        if worker_manager.scale_down_threshold >= worker_manager.scale_up_threshold:
            self.errors.append("Worker manager scale_down_threshold must be < scale_up_threshold")

    def _validate_registry_config(self, config: Config) -> None:
        """Validate registry configuration section

        Args:
            config: Configuration object
        """
        registry = config.registry

        if not isinstance(registry.enabled, bool):
            self.errors.append("Registry enabled must be a boolean")

        if registry.batch_size <= 0:
            self.errors.append("Registry batch_size must be positive")

        if registry.flush_interval <= 0:
            self.errors.append("Registry flush_interval must be positive")

        if registry.queue_size <= 0:
            self.errors.append("Registry queue_size must be positive")

        if not isinstance(registry.path, str):
            self.errors.append("Registry path must be a string")

    def _validate_skip_config(self, config: Config) -> None:
        """Validate gather-skip configuration section

        Args:
            config: Configuration object
        """
        skip = config.skip

        if skip.skip_known not in ("off", "shadow", "on"):
            self.errors.append("Skip skip_known must be one of: off, shadow, on")

        if skip.gather_ttl_hours <= 0:
            self.errors.append("Skip gather_ttl_hours must be positive")

        # Cross-check: the decision engine reads the registry; without it every
        # lookup fails open and the flag silently degrades to a no-op.
        if skip.skip_known != "off" and not config.registry.enabled:
            self.warnings.append(
                f"skip.skip_known='{skip.skip_known}' has no effect while registry.enabled is false "
                "(no registry data to read; all links fail open to re-gather)"
            )

    def _validate_enrichment_config(self, config: Config) -> None:
        """Validate repository metadata enrichment configuration section

        Args:
            config: Configuration object
        """
        enrichment = config.enrichment

        if not isinstance(enrichment.enabled, bool):
            self.errors.append("Enrichment enabled must be a boolean")

        if enrichment.ttl_hours <= 0:
            self.errors.append("Enrichment ttl_hours must be positive")

        # Cross-check: the cache lives in the registry; without it enrichment
        # has nowhere durable to read/write and silently degrades to a no-op.
        if enrichment.enabled and not config.registry.enabled:
            self.warnings.append(
                "enrichment.enabled=true has no effect while registry.enabled is false "
                "(no persistent repo cache; all fetches would be retried every encounter)"
            )

    def _validate_early_stop_config(self, config: Config) -> None:
        """Validate frontier-based early-stop configuration section

        Args:
            config: Configuration object
        """
        early_stop = config.early_stop

        if early_stop.mode not in ("off", "shadow", "on"):
            self.errors.append("Early stop mode must be one of: off, shadow, on")

        if early_stop.window <= 0:
            self.errors.append("Early stop window must be positive")

        if not (0.5 <= early_stop.theta <= 1.0):
            self.errors.append("Early stop theta must be between 0.5 and 1.0")

        if early_stop.min_pages < 1:
            self.errors.append("Early stop min_pages must be at least 1")

        if early_stop.min_trust < 0:
            self.errors.append("Early stop min_trust must be non-negative")

        # Cross-check: the detector reads the registry for both "known"
        # classification and the trust gate; without it every evaluation fails
        # open and the flag silently degrades to a no-op.
        if early_stop.mode != "off" and not config.registry.enabled:
            self.warnings.append(
                f"early_stop.mode='{early_stop.mode}' has no effect while registry.enabled is false "
                "(no registry data to classify; all windows fail open to full passes)"
            )

    def _validate_rate_limits(self, config: Config) -> None:
        """Validate rate limits configuration

        Args:
            config: Configuration object
        """
        for name, rate_limit in config.ratelimits.items():
            if rate_limit.base_rate <= 0:
                self.errors.append(f"Base rate must be positive for rate limit: {name}")

            if rate_limit.burst_limit <= 0:
                self.errors.append(f"Burst limit must be positive for rate limit: {name}")

            if not (0 < rate_limit.backoff_factor < 1):
                self.errors.append(f"Backoff factor must be between 0 and 1 for rate limit: {name}")

    def _validate_display_config(self, config: Config) -> None:
        """Validate display configuration

        Args:
            config: Configuration object to validate
        """
        if not config.display:
            self.errors.append("Display configuration is missing")
            return

        if not config.display.contexts:
            self.errors.append("Display contexts configuration is missing")
            return

        # Validate each context and mode
        for context_name, context_modes in config.display.contexts.items():
            if not context_modes:
                self.errors.append(f"No display modes configured for context: {context_name}")
                continue

            for mode_name, mode_config in context_modes.items():
                prefix = f"Display config [{context_name}.{mode_name}]"

                # Validate width
                if mode_config.width <= 0:
                    self.errors.append(f"{prefix}: width must be positive")
                elif mode_config.width < 40:
                    self.errors.append(f"{prefix}: width should be at least 40 characters")
                elif mode_config.width > 200:
                    self.errors.append(f"{prefix}: width should not exceed 200 characters")

                # Validate max_alerts_per_level
                if mode_config.max_alerts_per_level <= 0:
                    self.errors.append(f"{prefix}: max_alerts_per_level must be positive")
                elif mode_config.max_alerts_per_level > 20:
                    self.errors.append(f"{prefix}: max_alerts_per_level should not exceed 20")

                # Validate title
                if not mode_config.title.strip():
                    self.errors.append(f"{prefix}: title cannot be empty")
