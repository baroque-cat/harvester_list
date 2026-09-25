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

from constant import search as search_constants

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

        # Validate query syntax against the chosen transport (advisory lint)
        self._validate_query_transport_compat(config)

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

        # Validate inline key check-skip configuration
        self._validate_check_skip_config(config)

        # Validate periodic key re-check configuration
        self._validate_recheck_config(config)

        # Validate repository prioritization configuration
        self._validate_prioritization_config(config)

        # Validate shared search-response aggregation configuration
        self._validate_aggregation_config(config)

        # Validate search-work fan-out governor configuration
        self._validate_refine_governor_config(config)

        # Validate durable task-queue backend configuration
        self._validate_task_queue_config(config)

        # Validate gather transport configuration
        self._validate_gather_config(config)

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

        if pipeline.failure_handling not in ("legacy", "shadow", "strict"):
            self.errors.append("Pipeline failure_handling must be one of: legacy, shadow, strict")

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

    def _validate_query_transport_compat(self, config: Config) -> None:
        """Warn about web-only qualifiers under API transport (advisory lint).

        The GitHub code-search REST API silently treats web-only qualifiers as
        zero matches (live probe recorded in ``constant/search.py``).  This is a
        warning, never an error: wire behavior is a moving target, so the config
        stays loadable and the warning merely goes stale if the API changes.
        The qualifier list is read dynamically from ``constant.search`` so it
        can be extended without touching this logic (config-query-lint S4).
        """
        qualifiers = getattr(search_constants, "WEB_ONLY_QUALIFIERS", ())
        if not qualifiers:
            return

        for task in config.tasks:
            if not getattr(task, "enabled", False) or not getattr(task, "use_api", False):
                continue
            for index, condition in enumerate(task.conditions or []):
                query = (getattr(condition, "query", "") or "").lower()
                for qualifier in qualifiers:
                    if qualifier.lower() in query:
                        self.warnings.append(
                            f"Task '{task.name}' condition {index + 1}: query contains web-only "
                            f"qualifier '{qualifier}', which the GitHub code-search API ignores "
                            f"(transport: api) and returns zero matches for. Use an API-compatible "
                            f"query, or a web provider (use_api: false)."
                        )

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

    def _validate_check_skip_config(self, config: Config) -> None:
        """Validate inline key check-skip configuration section."""
        check_skip = config.check_skip

        if check_skip.mode not in ("off", "on"):
            self.errors.append("Check skip mode must be one of: off, on")

        for status, hours in (check_skip.ttl_hours or {}).items():
            if hours <= 0:
                self.errors.append(f"Check skip ttl_hours.{status} must be positive")

        # Cross-check: the ledger lives in the registry; without it the skip
        # lookup fails open every time and the flag silently degrades to a no-op.
        if check_skip.mode != "off" and not config.registry.enabled:
            self.warnings.append(
                "check_skip.mode='on' has no effect while registry.enabled is false "
                "(no ledger data to read; every key fails open to a provider check)"
            )

    def _validate_recheck_config(self, config: Config) -> None:
        """Validate periodic key re-check configuration section."""
        recheck = config.recheck

        if not isinstance(recheck.enabled, bool):
            self.errors.append("Recheck enabled must be a boolean")

        if recheck.interval_hours <= 0:
            self.errors.append("Recheck interval_hours must be positive")

        if recheck.batch_size <= 0:
            self.errors.append("Recheck batch_size must be positive")

        if recheck.enabled and not config.registry.enabled:
            self.warnings.append(
                "recheck.enabled=true has no effect while registry.enabled is false "
                "(the ledger lives in the registry; no keys to select)"
            )

    def _validate_prioritization_config(self, config: Config) -> None:
        """Validate repository prioritization configuration section."""
        prioritization = config.prioritization

        for name in ("w1", "w2", "w4", "w5"):
            if float(getattr(prioritization, name)) < 0:
                self.errors.append(f"Prioritization {name} must be non-negative")

        if prioritization.half_life_days <= 0:
            self.errors.append("Prioritization half_life_days must be positive")

        if prioritization.threshold_kb < 0:
            self.errors.append("Prioritization threshold_kb must be non-negative")

        if prioritization.ramp_kb <= prioritization.threshold_kb:
            self.errors.append("Prioritization ramp_kb must be > threshold_kb")

        if prioritization.display_top_n < 0:
            self.errors.append("Prioritization display_top_n must be non-negative")

    def _validate_aggregation_config(self, config: Config) -> None:
        """Validate shared search-response aggregation configuration section.

        Misconfiguration here is loud: an unknown mode or an out-of-range TTL
        would either silently disable sharing or expose stale data beyond the
        measured stability horizon, so these are errors rather than warnings.
        """
        aggregation = config.aggregation

        if aggregation.mode not in ("off", "shadow", "on"):
            self.errors.append("Aggregation mode must be one of: off, shadow, on")

        for name in ("ttl_web_s", "ttl_api_s"):
            value = float(getattr(aggregation, name))
            if not (1 <= value <= 3600):
                self.errors.append(f"Aggregation {name} must be between 1 and 3600")

        if int(aggregation.max_bytes) < 1024 * 1024:
            self.errors.append("Aggregation max_bytes must be at least 1 MiB")

        join_timeout = float(aggregation.join_timeout_s)
        if not (1 <= join_timeout <= 600):
            self.errors.append("Aggregation join_timeout_s must be between 1 and 600")

    def _validate_refine_governor_config(self, config: Config) -> None:
        """Validate the search-work fan-out governor configuration section.

        Misconfiguration here is loud: an unknown mode can silently disable
        enforcement (the ungoverned state is the recorded OOM incident), and a
        non-positive cap would make the governor refuse every child.
        """
        governor = config.refine_governor

        if governor.mode not in ("off", "shadow", "on"):
            self.errors.append("RefineGovernor mode must be one of: off, shadow, on")

        for name in ("max_refine_depth", "max_partitions_per_refine"):
            value = int(getattr(governor, name))
            if value <= 0:
                self.errors.append(f"RefineGovernor {name} must be positive")

        # The run budget is a cap like the others: spec requirement 5 fails
        # non-positive caps loudly.  (``RefineGovernorConfig.__post_init__``
        # raises first for loader-built configs; this keeps direct construction
        # honest too.)
        if int(governor.max_search_tasks_per_run) <= 0:
            self.errors.append("RefineGovernor max_search_tasks_per_run must be positive")

        if int(governor.max_refine_depth) > 5:
            self.errors.append("RefineGovernor max_refine_depth must be at most 5")

    def _validate_task_queue_config(self, config: Config) -> None:
        """Validate the durable task-queue backend configuration section.

        Misconfiguration here is loud: an unknown backend would silently fall
        back to a different durability story, and a non-positive timeout/age
        would make claims unrescuable or purge everything on startup.
        """
        queue_config = config.queue

        if queue_config.backend not in ("memory", "sqlite"):
            self.errors.append("Queue backend must be one of: memory, sqlite")

        if float(queue_config.visibility_timeout_s) <= 0:
            self.errors.append("Queue visibility_timeout_s must be positive")

        if float(queue_config.max_age_hours) <= 0:
            self.errors.append("Queue max_age_hours must be positive")

    def _validate_gather_config(self, config: Config) -> None:
        """Validate the gather transport configuration section.

        Re-checks the enum and positivity constraints enforced by the dataclass
        (they can be bypassed by attribute mutation) and adds the cross-section
        durability invariant from design D5: a refusal wait at or beyond the
        durable queue's visibility timeout guarantees a duplicate execution
        because the queue exposes no claim renewal.
        """
        gather_config = config.gather

        transport = str(gather_config.transport).strip().lower()
        if transport not in ("html", "raw", "rest"):
            self.errors.append(f"gather.transport must be one of: html, raw, rest (got: {gather_config.transport!r})")

        if int(gather_config.max_payload_bytes) <= 0:
            self.errors.append("Gather max_payload_bytes must be positive")

        wait_cap = float(gather_config.max_refusal_wait_s)
        if wait_cap <= 0:
            self.errors.append("Gather max_refusal_wait_s must be positive")

        # Cross-section durability invariant (only meaningful for the durable
        # backend; the in-memory queue has no visibility window).
        if config.queue.backend == "sqlite":
            visibility = float(config.queue.visibility_timeout_s)
            if wait_cap >= visibility:
                self.errors.append(
                    f"Gather max_refusal_wait_s ({gather_config.max_refusal_wait_s}) must be "
                    f"strictly less than queue.visibility_timeout_s ({config.queue.visibility_timeout_s}) "
                    f"so no worker sleeps inside a claimed durable row past its visibility window"
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
