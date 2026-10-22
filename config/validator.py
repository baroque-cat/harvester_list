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

# Module-level logger: created at import so non-propagating capture (pytest
# caplog / live logging) sees the category before the first message.  Safe now
# that tools.coordinator no longer imports config at module level (the former
# cycle blocked any config/* -> tools.logger import).
from tools.logger import get_logger

from .schemas import Config, LoadBalanceStrategy, TaskConfig

logger = get_logger("config")


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

        # Validate file commit-date acquisition configuration
        self._validate_file_dating_config(config)

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

        # Validate search-stage cross-provider share configuration
        self._validate_search_config(config)

        # Validate credential-liveness / bounded-wait configuration
        self._validate_credential_liveness_config(config)

        # Validate LLM-provider refusal-classification configuration
        self._validate_provider_config(config)

        # Validate acquisition-task recovery source configuration
        self._validate_recovery_config(config)

        # Validate rate limits
        self._validate_rate_limits(config)

        # Validate display configuration
        self._validate_display_config(config)

        # Validate persistence/snapshot configuration
        self._validate_persistence_config(config)

        # Validate failure-handling mode coupling (NEW-1: shadow + registry-write hazard)
        self._validate_failure_handling_coupling(config)

        # Validate handle-coherence advisories (W1–W5: cross-section budget/ledger/age/envelope/pattern checks)
        self._validate_handle_coherence(config)

        # Surface non-fatal configuration warnings.
        if self.warnings:
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

            # Validate dedup sizes
            if stage not in pipeline.dedup_sizes:
                self.errors.append(f"Missing dedup size for stage: {stage}")
            elif pipeline.dedup_sizes[stage] <= 0:
                self.errors.append(f"Dedup size for {stage} must be positive")

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

        if registry.decision_log_max_bytes <= 0:
            self.errors.append("Registry decision_log_max_bytes must be positive")

        if registry.decision_log_keep_rotated <= 0:
            self.errors.append("Registry decision_log_keep_rotated must be positive")

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

    def _validate_file_dating_config(self, config: Config) -> None:
        """Validate file commit-date acquisition configuration section.

        Mirrors FileDatingConfig.__post_init__ (deliberate duplication): the
        validator collects errors instead of raising so an operator sees every
        misconfiguration in one load.
        """
        fd = config.file_dating

        if not isinstance(fd.enabled, bool):
            self.errors.append("file_dating.enabled must be a boolean")

        transport = str(fd.transport).strip().lower()
        if transport not in ("rest", "graphql"):
            self.errors.append("file_dating.transport must be one of: rest, graphql")

        scope = str(fd.scope).strip().lower()
        if scope not in ("keyed", "gathered", "discovered"):
            self.errors.append("file_dating.scope must be one of: keyed, gathered, discovered")

        ttl = float(fd.ttl_hours)
        if ttl <= 0:
            self.errors.append("file_dating.ttl_hours must be positive")

        max_req = int(fd.max_requests_per_run)
        if max_req < 0:
            self.errors.append("file_dating.max_requests_per_run must be non-negative")

        batch_size = int(fd.graphql_batch_size)
        if batch_size < 1:
            self.errors.append("file_dating.graphql_batch_size must be at least 1")

        batch_max = int(fd.graphql_batch_max)
        if batch_max < 1:
            self.errors.append("file_dating.graphql_batch_max must be at least 1")

        if batch_size > batch_max:
            self.errors.append(
                "file_dating.graphql_batch_size must not exceed file_dating.graphql_batch_max"
            )

        # Cross-check: dating results live in the registry; without it the
        # cache has nowhere durable to read/write and silently degrades to
        # re-fetching on every encounter.
        if fd.enabled and not config.registry.enabled:
            self.warnings.append(
                "file_dating.enabled=true has no effect while registry.enabled is false "
                "(no persistent date cache; all fetches would be retried every encounter)"
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

        # Mirrors RefineGovernorConfig.__post_init__ (deliberate duplication):
        # a non-boolean value would silently invert the rollback flag, so it
        # fails loudly and names the key.  No coercion (design D8).
        if not isinstance(governor.drop_wire_indistinguishable, bool):
            self.errors.append(
                "refine_governor.drop_wire_indistinguishable must be a boolean"
            )

        # Coverage-probe rollout flag (slice 3a.2, F-05).
        if governor.coverage_mode not in ("off", "shadow", "on"):
            self.errors.append(
                "refine_governor.coverage_mode must be one of: off, shadow, on"
            )

        # Wire-coverage TTL (slice 3a.2, F-06).
        if int(governor.wire_ttl_hours) <= 0:
            self.errors.append(
                "refine_governor.wire_ttl_hours must be positive"
            )

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

        # Mirrors TaskQueueConfig.__post_init__ (deliberate duplication): a
        # non-boolean value would silently invert the rollback flag, so it fails
        # loudly and names the key.  No coercion (design D6).
        if not isinstance(queue_config.durable_dedup, bool):
            self.errors.append("queue.durable_dedup must be a boolean")

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

        # Bool only, loud: ``false`` is a supported rollback position (legacy
        # classification), not a hazard, so it earns no warning (design D5).
        if not isinstance(gather_config.defer_local_suppression, bool):
            self.errors.append(
                f"gather.defer_local_suppression must be a boolean "
                f"(got: {gather_config.defer_local_suppression!r})"
            )

        # F5: conditional_refetch — no coercion, mirrored from __post_init__.
        if not isinstance(gather_config.conditional_refetch, bool):
            self.errors.append("gather.conditional_refetch must be a boolean")

        # R7 D9: share_fetch — deliberate duplication of __post_init__ check
        # (house doctrine: validator collects all errors, dataclass raises on
        # first; both paths must produce byte-identical messages).
        share_mode = str(gather_config.share_fetch).strip().lower()
        if share_mode not in ("off", "shadow", "on"):
            self.errors.append("gather.share_fetch must be one of: off, shadow, on")

        # Slice 2.1 (C2): positive-int cap on tracked keys.
        if int(gather_config.share_ledger_max_tracked) <= 0:
            self.errors.append("gather.share_ledger_max_tracked must be positive")

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

    def _validate_search_config(self, config: Config) -> None:
        """Validate the search-stage cross-provider share configuration section.

        Re-checks the enum and positivity constraints enforced by the dataclass
        (they can be bypassed by attribute mutation).  Byte-identical messages
        to ``SearchConfig.__post_init__`` (house doctrine: validator collects
        all errors, dataclass raises on first).
        """
        search_config = config.search

        # share_fetch — deliberate duplication of __post_init__ check.
        share_mode = str(search_config.share_fetch).strip().lower()
        if share_mode not in ("off", "shadow", "on"):
            self.errors.append("search.share_fetch must be one of: off, shadow, on")

        # Positive-int cap on tracked keys.
        if int(search_config.share_ledger_max_tracked) <= 0:
            self.errors.append("search.share_ledger_max_tracked must be positive")

    def _validate_provider_config(self, config: Config) -> None:
        """Validate the LLM-provider refusal-classification configuration section.

        Re-checks the bool/positivity constraints enforced by the dataclass (they
        can be bypassed by attribute mutation) and adds the cross-section
        durability invariant from design D7: a refusal wait at or beyond the
        durable queue's visibility timeout guarantees a duplicate execution
        because the queue exposes no claim renewal.
        """
        provider = config.provider

        # Bool only, loud: ``false`` is the supported rollback position (legacy
        # classification), not a hazard, so it earns no warning (design D7).
        if not isinstance(provider.classify_refusals, bool):
            self.errors.append(
                f"provider.classify_refusals must be a boolean "
                f"(got: {provider.classify_refusals!r})"
            )

        wait_cap = float(provider.max_refusal_wait_s)
        if wait_cap <= 0:
            self.errors.append("Provider max_refusal_wait_s must be positive")

        # Cross-section durability invariant (only meaningful for the durable
        # backend; the in-memory queue has no visibility window).
        if config.queue.backend == "sqlite":
            visibility = float(config.queue.visibility_timeout_s)
            if wait_cap >= visibility:
                self.errors.append(
                    f"provider.max_refusal_wait_s ({provider.max_refusal_wait_s}) must be "
                    f"strictly less than queue.visibility_timeout_s ({config.queue.visibility_timeout_s}) "
                    f"so no worker sleeps inside a claimed durable row past its visibility window"
                )

    def _validate_recovery_config(self, config: Config) -> None:
        """Validate the acquisition-task recovery source configuration section.

        Re-checks the enum/non-negativity constraints enforced by the dataclass
        (they can be bypassed by attribute mutation).  The dataclass messages
        are mirrored byte-for-byte so direct construction and loader paths
        report identically.
        """
        recovery = config.recovery

        if recovery.source not in ("shards", "registry", "auto"):
            self.errors.append(
                "recovery.source must be one of: shards, registry, auto"
            )
        if not isinstance(recovery.per_boot_cap, int) or recovery.per_boot_cap < 0:
            self.errors.append(
                "recovery.per_boot_cap must be a non-negative integer"
            )

    def _validate_credential_liveness_config(self, config: Config) -> None:
        """Validate the credential-liveness / bounded-wait configuration section.

        Re-checks the enum/positivity constraints enforced by the dataclass (they
        can be bypassed by attribute mutation) and adds the cross-section
        durability invariant from design D2: under the durable sqlite backend a
        bounded credential wait at or beyond the queue visibility timeout
        guarantees a duplicate execution because the queue exposes no claim
        renewal.  ``wait_mode=blocking`` deliberately re-accepts that hazard for
        rollback, so it is surfaced as a loud WARNING instead of an error.
        """
        liveness = config.credential_liveness

        wait_mode = str(liveness.wait_mode).strip().lower()
        if wait_mode not in ("bounded", "blocking"):
            self.errors.append("credential_liveness.wait_mode must be one of: bounded, blocking")

        max_wait = float(liveness.max_wait_s)
        if max_wait <= 0:
            self.errors.append("credential_liveness.max_wait_s must be positive")

        if int(liveness.emergency_threshold) <= 0:
            self.errors.append("credential_liveness.emergency_threshold must be positive")

        # Cross-section durability invariant (only meaningful for the durable
        # backend; the in-memory queue has no visibility window).
        if config.queue.backend == "sqlite":
            visibility = float(config.queue.visibility_timeout_s)
            if wait_mode == "blocking":
                self.warnings.append(
                    "credential_liveness.wait_mode=blocking under queue.backend=sqlite "
                    "deliberately re-accepts the duplicate-execution hazard: an in-task wait may "
                    "outlive the visibility window because the queue exposes no claim renewal"
                )
            elif max_wait >= visibility:
                self.errors.append(
                    f"credential_liveness.max_wait_s ({liveness.max_wait_s}) must be strictly less "
                    f"than queue.visibility_timeout_s ({config.queue.visibility_timeout_s}) "
                    f"so no in-task credential wait outlives its durable claim window"
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

    def _validate_persistence_config(self, config: Config) -> None:
        """Validate persistence/snapshot configuration section.

        Mirrors PersistenceConfig.__post_init__ (deliberate duplication): the
        validator collects all errors instead of raising on first, so an
        operator sees every misconfiguration in one load.
        """
        persistence = config.persistence

        if int(persistence.batch_size) <= 0:
            self.errors.append("persistence.batch_size must be positive")

        if int(persistence.save_interval) <= 0:
            self.errors.append("persistence.save_interval must be positive")

        if int(persistence.queue_interval) <= 0:
            self.errors.append("persistence.queue_interval must be positive")

        if int(persistence.shutdown_timeout) <= 0:
            self.errors.append("persistence.shutdown_timeout must be positive")

        if int(persistence.snapshot_interval_s) <= 0:
            self.errors.append("persistence.snapshot_interval_s must be positive")

        if int(persistence.snapshot_keep_last_n) < 0:
            self.errors.append("persistence.snapshot_keep_last_n must be non-negative")

        if int(persistence.snapshot_final_max_bytes) <= 0:
            self.errors.append("persistence.snapshot_final_max_bytes must be positive")

    def _validate_failure_handling_coupling(self, config: Config) -> None:
        """Validate failure-handling mode coupling with registry-write surface (NEW-1).

        The shadow mode combined with registry-write (skip_known != off) creates
        a latent poisoning hazard: swallowed TransientFetchErrors write sticky
        gathered_ok status that suppresses re-gathering for 168 hours.  This is
        a WARNING (advisory), not an error — the config still loads, but the
        operator is alerted to the hazard.

        Traceability: NEW-1 (02_critical_fixes.md), failure-handling spec §1.4 delta.
        """
        mode = config.pipeline.failure_handling
        skip_mode = config.skip.skip_known

        # The hazard exists only when:
        # 1. failure_handling=shadow (swallows transients without raising)
        # 2. skip_known != off (registry writes are active consumers)
        # 3. registry is enabled (the poisoning surface exists)
        if mode == "shadow" and skip_mode != "off" and config.registry.enabled:
            self.warnings.append(
                f"failure_handling=shadow combined with skip.skip_known={skip_mode} "
                "creates a sticky gathered_ok poisoning hazard: swallowed TransientFetchErrors "
                "write visit_status='gathered_ok' to the registry, which suppresses re-gathering "
                "for 168 hours via skip_known and is never downgraded by the sticky SQL CASE. "
                "Consider failure_handling=strict (records 'failed' on transient errors) or "
                "skip.skip_known=off (no registry-write consumer) to avoid the hazard."
            )

    def _validate_handle_coherence(self, config: Config) -> None:
        """Validate handle-coherence advisories (W1–W5, slice 2.7).

        Cross-section checks that alert the operator to configuration mismatches
        that degrade program goals (coverage, provider aggregation, link accumulation)
        without blocking config load (all WARNING-class, house advisory style).

        W1: budget < N_enabled_tasks × clamp — budget starves some roots in the same run.
        W2: share_ledger_max_tracked < proxy — ledger cap smaller than expected distinct
            gather keys in backlog ⇒ share suppression degrades to counted-fail-open.
            Proxy: refine_governor.max_search_tasks_per_run (search budget bounds distinct
            keys discovered; default 10000 ≤ default ledger 50000 ⇒ neutral).
        W3: max_age_hours < HORIZON_HOURS (456 = 19 days × 24, Д3) — age gate narrower
            than program horizon ⇒ tasks age out before full coverage.
        W4: max_partitions_per_refine > 46656 (36^3 envelope, AGENTS §0.1) — fat-finger
            guard; WARNING only (F8: no hard upper bound).
        W5: ≥2 distinct key_pattern sets among enabled tasks — share suppression between
            providers with different patterns is structurally impossible (goal 2 narrows).

        Traceability: 02_critical_fixes.md:48 (W1–W5 line), 02_critical_fixes.md:98 (F8),
        08_decisions.md Д3 (19 days / 10^6 wires), AGENTS.md §0.1 (46656 = 36^3).
        """
        enabled_tasks = [t for t in config.tasks if t.enabled]
        n_enabled = len(enabled_tasks)

        # W1: budget < N_enabled × clamp (budget starvation)
        if n_enabled >= 2:
            budget = int(config.refine_governor.max_search_tasks_per_run)
            clamp = int(config.refine_governor.max_partitions_per_refine)
            demand = n_enabled * clamp
            if budget < demand:
                self.warnings.append(
                    f"W1: refine_governor.max_search_tasks_per_run ({budget}) is less than "
                    f"N_enabled_tasks × max_partitions_per_refine ({n_enabled} × {clamp} = {demand}); "
                    f"the budget starves some roots in the same run. "
                    f"Raise max_search_tasks_per_run to ≥ {demand} or reduce max_partitions_per_refine."
                )

        # W2: share_ledger_max_tracked < proxy (ledger undersized)
        ledger_cap = int(config.gather.share_ledger_max_tracked)
        proxy = int(config.refine_governor.max_search_tasks_per_run)
        if ledger_cap < proxy:
            self.warnings.append(
                f"W2: gather.share_ledger_max_tracked ({ledger_cap}) is less than "
                f"refine_governor.max_search_tasks_per_run ({proxy}); "
                f"the ledger cap is smaller than the expected distinct gather keys in backlog, "
                f"so share suppression degrades to counted-fail-open. "
                f"Raise share_ledger_max_tracked to ≥ {proxy}."
            )

        # W3: max_age_hours < HORIZON_HOURS (age gate narrower than program horizon)
        HORIZON_HOURS = 456  # 19 days × 24 (08_decisions.md Д3: program horizon 19 days / 10^6 wires)
        age_gate = float(config.queue.max_age_hours)
        if age_gate < HORIZON_HOURS:
            self.warnings.append(
                f"W3: queue.max_age_hours ({age_gate}) is less than the program horizon "
                f"({HORIZON_HOURS} hours = 19 days, Д3); "
                f"tasks age out before full coverage. "
                f"Raise max_age_hours to ≥ {HORIZON_HOURS}."
            )

        # W4: max_partitions_per_refine > 46656 (fat-finger guard, 36^3 envelope)
        GENERATOR_ENVELOPE = 46_656  # 36^3 = three contiguous literals (AGENTS.md §0.1 table)
        clamp = int(config.refine_governor.max_partitions_per_refine)
        if clamp > GENERATOR_ENVELOPE:
            self.warnings.append(
                f"W4: refine_governor.max_partitions_per_refine ({clamp}) exceeds the generator "
                f"envelope ({GENERATOR_ENVELOPE} = 36^3, AGENTS §0.1); "
                f"this is a fat-finger guard, not a hard limit (F8). "
                f"Verify this is intentional."
            )

        # W5: ≥2 distinct key_pattern sets among enabled tasks (share suppression impossible)
        if n_enabled >= 2:
            pattern_sets = {t.patterns.key_pattern for t in enabled_tasks}
            if len(pattern_sets) >= 2:
                task_names = [t.name for t in enabled_tasks]
                self.warnings.append(
                    f"W5: enabled tasks {task_names} have {len(pattern_sets)} distinct key_pattern sets; "
                    f"share suppression between providers with different patterns is structurally "
                    f"impossible (goal 2 narrows). "
                    f"Align key_pattern across enabled tasks or accept reduced sharing."
                )
