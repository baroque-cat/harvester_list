#!/usr/bin/env python3

"""
Configuration Loader

This module provides unified YAML configuration loading functionality.
It replaces the scattered configuration loading logic throughout the application.

Key Features:
- Single YAML file loading
- Default configuration generation
- Type-safe configuration parsing
- Validation integration
"""

import os
from typing import Any, Dict

import yaml

from core.models import Condition, Patterns, RateLimitConfig, inherit_patterns

from .defaults import get_default_config
from .schemas import (
    AggregationConfig,
    ApiConfig,
    CheckSkipConfig,
    Config,
    CredentialsConfig,
    DisplayConfig,
    DisplayContextConfig,
    EarlyStopConfig,
    EnrichmentConfig,
    GatherConfig,
    GlobalConfig,
    LoadBalanceStrategy,
    MonitoringConfig,
    PersistenceConfig,
    PipelineConfig,
    PrioritizationConfig,
    RecheckConfig,
    RefineGovernorConfig,
    RegistryConfig,
    SkipConfig,
    StageConfig,
    StorageConfig,
    TaskConfig,
    TaskQueueConfig,
    WorkerManagerConfig,
)
from .validator import ConfigValidator


class ConfigLoader:
    """Unified configuration loader for YAML files"""

    def __init__(self, config_file: str = "config.yaml"):
        """Initialize configuration loader

        Args:
            config_file: Path to configuration file
        """
        self.config_file = config_file
        self.validator = ConfigValidator()

    def load(self) -> Config:
        """Load configuration from YAML file

        Returns:
            Config: Loaded and validated configuration object
        """
        if not os.path.exists(self.config_file):
            self._create_default_config()

        try:
            with open(self.config_file, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            config = self._parse_config(data)
            self.validator.validate(config)
            return config

        except Exception as e:
            raise RuntimeError(f"Failed to load config file {self.config_file}: {e}")

    def _parse_config(self, data: Dict[str, Any]) -> Config:
        """Parse YAML data into Config object

        Args:
            data: Raw YAML data

        Returns:
            Config: Parsed configuration object
        """
        config = Config()

        # Parse global configuration
        if "global" in data:
            config.global_config = self._parse_global_config(data["global"])

        # Parse pipeline configuration
        if "pipeline" in data:
            config.pipeline = self._parse_pipeline_config(data["pipeline"])

        # Parse monitoring configuration
        if "monitoring" in data:
            config.monitoring = self._parse_monitoring_config(data["monitoring"])

        # Parse display configuration
        if "display" in data:
            config.display = self._parse_display_config(data["display"])

        # Parse persistence configuration
        if "persistence" in data:
            config.persistence = self._parse_persistence_config(data["persistence"])

        # Parse worker configuration
        if "worker" in data:
            config.worker = self._parse_worker_manager_config(data["worker"])

        # Parse registry configuration
        if "registry" in data:
            config.registry = self._parse_registry_config(data["registry"])

        # Parse gather-skip configuration
        if "skip" in data:
            config.skip = self._parse_skip_config(data["skip"])

        # Parse repository metadata enrichment configuration
        if "enrichment" in data:
            config.enrichment = self._parse_enrichment_config(data["enrichment"])

        # Parse frontier-based early-stop configuration
        if "early_stop" in data:
            config.early_stop = self._parse_early_stop_config(data["early_stop"])

        # Parse inline key check-skip configuration
        if "check_skip" in data:
            config.check_skip = self._parse_check_skip_config(data["check_skip"])

        # Parse periodic key re-check configuration
        if "recheck" in data:
            config.recheck = self._parse_recheck_config(data["recheck"])

        # Parse repository prioritization configuration
        if "prioritization" in data:
            config.prioritization = self._parse_prioritization_config(data["prioritization"])

        # Parse shared search-response aggregation configuration
        if "aggregation" in data:
            config.aggregation = self._parse_aggregation_config(data["aggregation"])

        # Parse search-work fan-out governor configuration
        if "refine_governor" in data:
            config.refine_governor = self._parse_refine_governor_config(data["refine_governor"])

        # Parse durable task-queue backend configuration
        if "queue" in data:
            config.queue = self._parse_task_queue_config(data["queue"])

        # Parse gather transport configuration
        if "gather" in data:
            config.gather = self._parse_gather_config(data["gather"])

        # Parse rate limits
        if "ratelimits" in data:
            config.ratelimits = self._parse_rate_limits(data["ratelimits"])

        # Parse tasks
        if "tasks" in data:
            config.tasks = [self._parse_task_config(task_data) for task_data in data["tasks"]]

        return config

    def _parse_global_config(self, data: Dict[str, Any]) -> GlobalConfig:
        """Parse global configuration section

        Args:
            data: Global configuration data

        Returns:
            GlobalConfig: Parsed global configuration
        """
        credentials_data = data.get("github_credentials", {})

        # Get sessions and tokens from config
        sessions = credentials_data.get("sessions") or []
        tokens = credentials_data.get("tokens") or []

        # Filter out placeholder values from config
        valid_sessions = [s for s in sessions if s and not s.startswith("your_")]
        valid_tokens = [t for t in tokens if t and not t.startswith("your_")]

        # If both valid sessions and tokens are empty, try to read from environment variables
        if not valid_sessions and not valid_tokens:
            # Try to read GitHub sessions from GITHUB_SESSIONS environment variable
            github_sessions = os.getenv("GITHUB_SESSIONS")
            if github_sessions:
                valid_sessions = [s.strip() for s in github_sessions.split(",") if s.strip()]

            # Try to read GitHub tokens from GITHUB_TOKENS environment variable
            github_tokens = os.getenv("GITHUB_TOKENS")
            if github_tokens:
                valid_tokens = [t.strip() for t in github_tokens.split(",") if t.strip()]

        credentials = CredentialsConfig(
            sessions=valid_sessions,
            tokens=valid_tokens,
            strategy=LoadBalanceStrategy(credentials_data.get("strategy", "round_robin")),
        )

        proxy = data.get("proxy") or self._get_env_ignore_case("https_proxy", "http_proxy")

        return GlobalConfig(
            workspace=os.path.abspath(data.get("workspace", "./data")),
            max_retries_requeued=data.get("max_retries_requeued", 3),
            proxy=proxy,
            github_credentials=credentials,
            user_agents=data.get("user_agents", []),
        )

    @staticmethod
    def _get_env_ignore_case(*names: str) -> str:
        """Read the first non-empty environment variable by name, ignoring case."""
        env = {key.lower(): value for key, value in os.environ.items()}
        for name in names:
            value = env.get(name.lower(), "")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def _parse_pipeline_config(self, data: Dict[str, Any]) -> PipelineConfig:
        """Parse pipeline configuration section

        Args:
            data: Pipeline configuration data

        Returns:
            PipelineConfig: Parsed pipeline configuration
        """
        return PipelineConfig(
            threads=data.get("threads", {}),
            queue_sizes=data.get("queue_sizes", {}),
            failure_handling=data.get("failure_handling", "shadow"),
        )

    def _parse_monitoring_config(self, data: Dict[str, Any]) -> MonitoringConfig:
        """Parse system monitoring configuration section

        Args:
            data: Monitoring configuration data

        Returns:
            MonitoringConfig: Parsed monitoring configuration
        """
        return MonitoringConfig(
            update_interval=data.get("update_interval", 2.0),
            error_threshold=data.get("error_threshold", 0.1),
            queue_threshold=data.get("queue_threshold", 1000),
            memory_threshold=data.get("memory_threshold", 1073741824),
            response_threshold=data.get("response_threshold", 5.0),
        )

    def _parse_display_config(self, data: Dict[str, Any]) -> DisplayConfig:
        """Parse display configuration section

        Args:
            data: Display configuration data

        Returns:
            DisplayConfig: Parsed display configuration
        """
        contexts = {}
        contexts_data = data.get("contexts", {})

        for context_name, modes_data in contexts_data.items():
            contexts[context_name] = {}
            for mode_name, mode_data in modes_data.items():
                contexts[context_name][mode_name] = DisplayContextConfig(
                    title=mode_data.get("title", ""),
                    show_workers=mode_data.get("show_workers", True),
                    show_alerts=mode_data.get("show_alerts", True),
                    show_performance=mode_data.get("show_performance", False),
                    show_newline_prefix=mode_data.get("show_newline_prefix", False),
                    width=mode_data.get("width", 80),
                    max_alerts_per_level=mode_data.get("max_alerts_per_level", 3),
                )

        return DisplayConfig(contexts=contexts)

    def _parse_persistence_config(self, data: Dict[str, Any]) -> PersistenceConfig:
        """Parse persistence configuration section

        Args:
            data: Persistence configuration data

        Returns:
            PersistenceConfig: Parsed persistence configuration
        """
        return PersistenceConfig(
            batch_size=data.get("batch_size", 50),
            save_interval=data.get("save_interval", 30),
            queue_interval=data.get("queue_interval", 60),
            auto_restore=data.get("auto_restore", True),
            shutdown_timeout=data.get("shutdown_timeout", 30),
            simple=data.get("format", "txt").strip().lower() == "txt",
        )

    def _parse_worker_manager_config(self, data: Dict[str, Any]) -> WorkerManagerConfig:
        """Parse worker manager configuration section

        Args:
            data: Worker manager configuration data

        Returns:
            WorkerManagerConfig: Parsed worker manager configuration
        """
        return WorkerManagerConfig(
            enabled=data.get("enabled", False),
            min_workers=data.get("min_workers", 1),
            max_workers=data.get("max_workers", 10),
            target_queue_size=data.get("target_queue_size", 100),
            adjustment_interval=data.get("adjustment_interval", 5.0),
            scale_up_threshold=data.get("scale_up_threshold", 0.8),
            scale_down_threshold=data.get("scale_down_threshold", 0.2),
            log_recommendations=data.get("log_recommendations", True),
        )

    def _parse_registry_config(self, data: Dict[str, Any]) -> RegistryConfig:
        """Parse registry configuration section

        Args:
            data: Registry configuration data

        Returns:
            RegistryConfig: Parsed registry configuration
        """
        return RegistryConfig(
            enabled=data.get("enabled", False),
            path=data.get("path", ""),
            batch_size=data.get("batch_size", 50),
            flush_interval=data.get("flush_interval", 5),
            queue_size=data.get("queue_size", 100000),
        )

    def _parse_skip_config(self, data: Dict[str, Any]) -> SkipConfig:
        """Parse gather-skip configuration section

        Args:
            data: Gather-skip configuration data

        Returns:
            SkipConfig: Parsed gather-skip configuration
        """
        return SkipConfig(
            skip_known=data.get("skip_known", "off"),
            gather_ttl_hours=data.get("gather_ttl_hours", 168.0),
        )

    def _parse_enrichment_config(self, data: Dict[str, Any]) -> EnrichmentConfig:
        """Parse repository metadata enrichment configuration section

        Args:
            data: Enrichment configuration data

        Returns:
            EnrichmentConfig: Parsed enrichment configuration
        """
        return EnrichmentConfig(
            enabled=data.get("enabled", False),
            ttl_hours=data.get("ttl_hours", 24.0),
        )

    def _parse_early_stop_config(self, data: Dict[str, Any]) -> EarlyStopConfig:
        """Parse frontier-based early-stop configuration section

        Args:
            data: Early-stop configuration data

        Returns:
            EarlyStopConfig: Parsed early-stop configuration
        """
        return EarlyStopConfig(
            mode=data.get("mode", "off"),
            window=data.get("window", 100),
            theta=data.get("theta", 0.9),
            min_pages=data.get("min_pages", 2),
            min_trust=data.get("min_trust", 1000),
        )

    def _parse_check_skip_config(self, data: Dict[str, Any]) -> CheckSkipConfig:
        """Parse inline key check-skip configuration section

        Args:
            data: Check-skip configuration data

        Returns:
            CheckSkipConfig: Parsed check-skip configuration
        """
        ttl = data.get("ttl_hours", {}) or {}
        return CheckSkipConfig(
            mode=data.get("mode", "off"),
            ttl_hours=dict(ttl),
        )

    def _parse_recheck_config(self, data: Dict[str, Any]) -> RecheckConfig:
        """Parse periodic key re-check configuration section

        Args:
            data: Re-check configuration data

        Returns:
            RecheckConfig: Parsed re-check configuration
        """
        return RecheckConfig(
            enabled=data.get("enabled", False),
            interval_hours=data.get("interval_hours", 6.0),
            batch_size=data.get("batch_size", 50),
        )

    def _parse_prioritization_config(self, data: Dict[str, Any]) -> PrioritizationConfig:
        """Parse repository prioritization configuration section

        Args:
            data: Prioritization configuration data

        Returns:
            PrioritizationConfig: Parsed prioritization configuration
        """
        return PrioritizationConfig(
            w1=data.get("w1", 100.0),
            w2=data.get("w2", 40.0),
            w4=data.get("w4", 30.0),
            half_life_days=data.get("half_life_days", 30.0),
            w5=data.get("w5", 20.0),
            threshold_kb=data.get("threshold_kb", 50_000.0),
            ramp_kb=data.get("ramp_kb", 500_000.0),
            display_top_n=data.get("display_top_n", 0),
        )

    def _parse_aggregation_config(self, data: Dict[str, Any]) -> AggregationConfig:
        """Parse shared search-response aggregation configuration section.

        Args:
            data: Aggregation configuration data

        Returns:
            AggregationConfig: Parsed aggregation configuration
        """
        return AggregationConfig(
            mode=data.get("mode", "off"),
            ttl_web_s=data.get("ttl_web_s", 120.0),
            ttl_api_s=data.get("ttl_api_s", 300.0),
            max_bytes=data.get("max_bytes", 64 * 1024 * 1024),
            join_timeout_s=data.get("join_timeout_s", 60.0),
        )

    def _parse_refine_governor_config(self, data: Dict[str, Any]) -> RefineGovernorConfig:
        """Parse the search-work fan-out governor configuration section.

        Args:
            data: Refine-governor configuration data

        Returns:
            RefineGovernorConfig: Parsed refine-governor configuration
        """
        return RefineGovernorConfig(
            mode=data.get("mode", "on"),
            max_refine_depth=data.get("max_refine_depth", 2),
            max_partitions_per_refine=data.get("max_partitions_per_refine", 128),
            max_search_tasks_per_run=data.get("max_search_tasks_per_run", 10000),
        )

    def _parse_task_queue_config(self, data: Dict[str, Any]) -> TaskQueueConfig:
        """Parse the durable task-queue backend configuration section.

        Args:
            data: Task-queue configuration data

        Returns:
            TaskQueueConfig: Parsed task-queue configuration
        """
        return TaskQueueConfig(
            backend=data.get("backend", "memory"),
            visibility_timeout_s=data.get("visibility_timeout_s", 300.0),
            max_age_hours=data.get("max_age_hours", 24.0),
        )

    def _parse_gather_config(self, data: Dict[str, Any]) -> GatherConfig:
        """Parse the gather transport configuration section.

        Unknown keys are ignored like the sibling parse methods; validation
        is delegated to the dataclass ``__post_init__``.

        Args:
            data: Gather configuration data

        Returns:
            GatherConfig: Parsed gather configuration
        """
        return GatherConfig(
            transport=data.get("transport", "raw"),
            max_payload_bytes=data.get("max_payload_bytes", 8 * 1024 * 1024),
            max_refusal_wait_s=data.get("max_refusal_wait_s", 60.0),
        )

    def _parse_rate_limits(self, data: Dict[str, Any]) -> Dict[str, RateLimitConfig]:
        """Parse rate limits configuration section

        Args:
            data: Rate limits configuration data

        Returns:
            Dict[str, RateLimitConfig]: Parsed rate limits
        """
        rate_limits = {}
        for name, limit_data in data.items():
            rate_limits[name] = RateLimitConfig(
                base_rate=limit_data.get("base_rate", 1.0),
                burst_limit=limit_data.get("burst_limit", 5),
                adaptive=limit_data.get("adaptive", True),
                backoff_factor=limit_data.get("backoff_factor", 0.5),
                recovery_factor=limit_data.get("recovery_factor", 1.1),
                max_rate_multiplier=limit_data.get("max_rate_multiplier", 2.0),
                min_rate_multiplier=limit_data.get("min_rate_multiplier", 0.1),
            )
        return rate_limits

    def _parse_task_config(self, data: Dict[str, Any]) -> TaskConfig:
        """Parse task configuration

        Args:
            data: Task configuration data

        Returns:
            TaskConfig: Parsed task configuration
        """
        # Parse stages
        stages_data = data.get("stages", {})
        stages = StageConfig(
            search=stages_data.get("search", True),
            gather=stages_data.get("gather", True),
            check=stages_data.get("check", True),
            inspect=stages_data.get("inspect", True),
        )

        # Parse API configuration
        api_data = data.get("api", {})
        api = ApiConfig(
            base_url=api_data.get("base_url", ""),
            completion_path=api_data.get("completion_path", "/v1/chat/completions"),
            model_path=api_data.get("model_path", "/v1/models"),
            default_model=api_data.get("default_model", ""),
            auth_key=api_data.get("auth_key", "Authorization"),
            extra_headers=api_data.get("extra_headers", {}),
            api_version=api_data.get("api_version", ""),
            timeout=api_data.get("timeout", 30),
            retries=api_data.get("retries", 3),
        )

        # Parse patterns
        patterns_data = data.get("patterns", {})
        patterns = Patterns(
            key_pattern=patterns_data.get("key_pattern", ""),
            address_pattern=patterns_data.get("address_pattern", ""),
            endpoint_pattern=patterns_data.get("endpoint_pattern", ""),
            model_pattern=patterns_data.get("model_pattern", ""),
        )

        # Parse conditions
        conditions_data = data.get("conditions", [])
        conditions = []
        for condition_data in conditions_data:
            condition = Condition.from_dict(condition_data)
            # Inherit global patterns if condition patterns are empty
            inherit_patterns(patterns, condition)
            conditions.append(condition)

        # Parse rate limit
        rate_limit_data = data.get("rate_limit", {})
        rate_limit = RateLimitConfig(
            base_rate=rate_limit_data.get("base_rate", 1.0),
            burst_limit=rate_limit_data.get("burst_limit", 5),
            adaptive=rate_limit_data.get("adaptive", True),
        )

        storage_data = data.get("storage", {})
        storage = StorageConfig(
            directory=storage_data.get("directory", ""),
            plan=storage_data.get("plan", ""),
        )

        return TaskConfig(
            name=data.get("name", ""),
            enabled=data.get("enabled", True),
            provider_type=data.get("provider_type", ""),
            use_api=data.get("use_api", False),
            stages=stages,
            storage=storage,
            extras=data.get("extras", {}),
            api=api,
            patterns=patterns,
            conditions=conditions,
            rate_limit=rate_limit,
        )

    def _create_default_config(self) -> None:
        """Create default configuration file"""
        default_config = get_default_config()

        with open(self.config_file, "w", encoding="utf-8") as f:
            yaml.dump(default_config, f, default_flow_style=False, allow_unicode=True, indent=2)

        print(f"Created default configuration file: {self.config_file}")
        print("Please edit the configuration file and set your GitHub credentials and other settings")
