#!/usr/bin/env python3

"""
Configuration Data Schemas

This module defines all configuration data classes used throughout the application.
It consolidates and replaces duplicate configuration definitions from multiple files.

Key Features:
- Type-safe configuration structures
- Default value support
- Validation methods
- Unified configuration schema
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.enums import LoadBalanceStrategy, PipelineStage
from core.models import Condition, Patterns, RateLimitConfig


@dataclass
class CredentialsConfig:
    """GitHub credentials configuration with load balancing"""

    sessions: List[str] = field(default_factory=list)
    tokens: List[str] = field(default_factory=list)
    strategy: LoadBalanceStrategy = LoadBalanceStrategy.ROUND_ROBIN

    def __post_init__(self):
        """Validate credentials configuration"""

        # Only require valid credentials if no placeholders are present
        if not isinstance(self.sessions, list):
            self.sessions = list()

        if not isinstance(self.tokens, list):
            self.tokens = list()

        # Convert string strategy to enum if needed
        if isinstance(self.strategy, str):
            self.strategy = LoadBalanceStrategy(self.strategy)


@dataclass
class GlobalConfig:
    """Global application configuration"""

    workspace: str = "./data"
    max_retries_requeued: int = 3
    proxy: str = ""
    github_credentials: Optional[CredentialsConfig] = None
    user_agents: List[str] = field(default_factory=list)

    def __post_init__(self):
        """Set default values if none provided"""
        self.proxy = self._normalize_proxy(self.proxy)

        # Set default credentials with placeholder values
        if self.github_credentials is None:
            self.github_credentials = CredentialsConfig(
                sessions=[],
                tokens=[],
                strategy=LoadBalanceStrategy.ROUND_ROBIN,
            )

        # Set default user agents if none provided
        if not self.user_agents:
            self.user_agents = [
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            ]

    @staticmethod
    def _normalize_proxy(proxy: Optional[str]) -> str:
        """Validate and normalize the global HTTP proxy URL."""
        if proxy is None:
            return ""

        if not isinstance(proxy, str):
            raise ValueError("proxy must be a string")

        proxy = proxy.strip()
        if not proxy:
            return ""

        parsed = urlparse(proxy)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https", "socks5"}:
            raise ValueError("proxy scheme must be one of: http, https, socks5")

        if not parsed.hostname:
            raise ValueError("proxy must include a host")

        # Accessing port validates the port syntax and range
        try:
            port = parsed.port
        except ValueError as e:
            raise ValueError(f"invalid proxy port: {e}") from e

        if scheme == "socks5" and port is None:
            raise ValueError("socks5 proxy must include a port")

        return proxy


def _get_default_threads() -> Dict[str, int]:
    """Get default thread configuration using StandardPipelineStage enum"""
    return {
        PipelineStage.SEARCH.value: 1,
        PipelineStage.GATHER.value: 8,
        PipelineStage.CHECK.value: 4,
        PipelineStage.INSPECT.value: 2,
    }


def _get_default_queue_sizes() -> Dict[str, int]:
    """Get default queue sizes using StandardPipelineStage enum"""
    return {
        PipelineStage.SEARCH.value: 100000,
        PipelineStage.GATHER.value: 200000,
        PipelineStage.CHECK.value: 500000,
        PipelineStage.INSPECT.value: 1000000,
    }


@dataclass
class PipelineConfig:
    """Pipeline stage configuration"""

    threads: Dict[str, int] = field(default_factory=_get_default_threads)
    queue_sizes: Dict[str, int] = field(default_factory=_get_default_queue_sizes)
    # Failure-handling contract: "legacy" | "shadow" | "strict" (project
    # paradigm: ship risky behavior behind a flag, default shadow at release).
    failure_handling: str = "shadow"

    def __post_init__(self):
        if not self.threads:
            self.threads = _get_default_threads()
        if not self.queue_sizes:
            self.queue_sizes = _get_default_queue_sizes()


@dataclass
class MonitoringConfig:
    """System monitoring and alerting configuration"""

    update_interval: float = 2.0
    error_threshold: float = 0.1
    queue_threshold: int = 1000
    memory_threshold: int = 1073741824  # 1GB in bytes
    response_threshold: float = 5.0

    def __post_init__(self):
        """Validate monitoring configuration"""
        if self.update_interval <= 0:
            raise ValueError("update_interval must be positive")
        if not (0 <= self.error_threshold <= 1):
            raise ValueError("error_threshold must be between 0 and 1")
        if self.queue_threshold < 0:
            raise ValueError("queue_threshold must be non-negative")
        if self.memory_threshold <= 0:
            raise ValueError("memory_threshold must be positive")
        if self.response_threshold <= 0:
            raise ValueError("response_threshold must be positive")

    def is_error_critical(self, error_rate: float) -> bool:
        """Check if error rate exceeds threshold"""
        return error_rate > self.error_threshold

    def is_queue_critical(self, queue_size: int) -> bool:
        """Check if queue size exceeds threshold"""
        return queue_size > self.queue_threshold

    def is_memory_critical(self, memory_usage_mb: int) -> bool:
        """Check if memory usage exceeds threshold"""
        return memory_usage_mb > self.memory_threshold

    def is_response_critical(self, response_time: float) -> bool:
        """Check if response time exceeds threshold"""
        return response_time > self.response_threshold


@dataclass
class DisplayContextConfig:
    """Display configuration for a specific context"""

    title: str = ""
    show_workers: bool = True
    show_alerts: bool = True
    show_performance: bool = False
    show_newline_prefix: bool = False

    # Formatting options
    width: int = 80
    max_alerts_per_level: int = 3


@dataclass
class DisplayConfig:
    """Display configuration for all contexts"""

    contexts: Dict[str, Dict[str, DisplayContextConfig]] = field(default_factory=dict)

    def __post_init__(self):
        """Set default display configurations if none provided"""
        if not self.contexts:
            self._set_default_contexts()

    def _set_default_contexts(self):
        """Set default display context configurations"""
        # System context
        self.contexts["system"] = {
            "standard": DisplayContextConfig(
                title="System Status", show_workers=True, show_alerts=True, show_performance=False
            ),
            "compact": DisplayContextConfig(
                title="System Status", show_workers=False, show_alerts=False, show_performance=False
            ),
            "detailed": DisplayContextConfig(
                title="Detailed System Status",
                show_workers=True,
                show_alerts=True,
                show_performance=True,
                show_newline_prefix=True,
            ),
        }

        # Monitoring context
        self.contexts["monitoring"] = {
            "standard": DisplayContextConfig(
                title="Pipeline Monitoring", show_workers=True, show_alerts=True, show_performance=True
            ),
            "detailed": DisplayContextConfig(
                title="Detailed Pipeline Monitoring",
                show_workers=True,
                show_alerts=True,
                show_performance=True,
                show_newline_prefix=True,
            ),
        }

        # Task manager context
        self.contexts["task"] = {
            "standard": DisplayContextConfig(
                title="Task Manager Status", show_workers=True, show_alerts=False, show_performance=False
            ),
            "compact": DisplayContextConfig(
                title="Task Manager Status", show_workers=False, show_alerts=False, show_performance=False
            ),
        }

        # Application context
        self.contexts["application"] = {
            "standard": DisplayContextConfig(
                title="Application Status", show_workers=False, show_alerts=True, show_performance=False
            ),
            "detailed": DisplayContextConfig(
                title="Detailed Application Status", show_workers=True, show_alerts=True, show_performance=True
            ),
        }

        # Main context
        self.contexts["main"] = {
            "standard": DisplayContextConfig(
                title="Pipeline Status", show_workers=True, show_alerts=False, show_performance=False
            ),
        }


@dataclass
class PersistenceConfig:
    """Persistence and recovery configuration"""

    batch_size: int = 50
    save_interval: int = 30
    queue_interval: int = 60
    snapshot_interval: int = 300  # seconds, periodic snapshot build interval
    auto_restore: bool = True
    shutdown_timeout: int = 30
    simple: bool = False  # Write simple text files alongside NDJSON


@dataclass
class RegistryConfig:
    """Persistent link registry configuration.

    The registry is write-only in this change: it records links, coverage and
    runs but never influences pipeline decisions.
    """

    enabled: bool = False
    path: str = ""  # empty => <workspace>/registry.sqlite
    batch_size: int = 50
    flush_interval: int = 5
    queue_size: int = 100000

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_registry_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding, while
        # the validator reports all config errors together for YAML loading.
        if not isinstance(self.enabled, bool):
            raise ValueError("registry.enabled must be a boolean")
        if not isinstance(self.path, str):
            raise ValueError("registry.path must be a string")
        if self.batch_size <= 0:
            raise ValueError("registry.batch_size must be positive")
        if self.flush_interval <= 0:
            raise ValueError("registry.flush_interval must be positive")
        if self.queue_size <= 0:
            raise ValueError("registry.queue_size must be positive")


@dataclass
class SkipConfig:
    """Registry-driven gather-skip configuration (add-gather-skip).

    ``skip_known`` is a three-position rollout flag: ``off`` performs no
    registry reads, ``shadow`` computes and logs decisions while still
    creating every task, and ``on`` enforces skips.
    """

    skip_known: str = "off"
    gather_ttl_hours: float = 168.0

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_skip_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding.
        mode = str(self.skip_known).strip().lower()
        if mode not in ("off", "shadow", "on"):
            raise ValueError("skip.skip_known must be one of: off, shadow, on")
        self.skip_known = mode

        self.gather_ttl_hours = float(self.gather_ttl_hours)
        if self.gather_ttl_hours <= 0:
            raise ValueError("skip.gather_ttl_hours must be positive")


@dataclass
class EnrichmentConfig:
    """Repository metadata enrichment configuration (add-repo-meta-enrichment).

    ``enabled`` defaults to off so the subsystem is byte-for-byte inert until an
    operator opts in.  ``ttl_hours`` bounds how long a cached ``repos`` row is
    trusted before a conditional refresh is attempted.
    """

    enabled: bool = False
    ttl_hours: float = 24.0

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_enrichment_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding.
        if not isinstance(self.enabled, bool):
            raise ValueError("enrichment.enabled must be a boolean")
        self.ttl_hours = float(self.ttl_hours)
        if self.ttl_hours <= 0:
            raise ValueError("enrichment.ttl_hours must be positive")


@dataclass
class EarlyStopConfig:
    """Frontier-based API pagination early-stop configuration.

    ``mode`` is the three-position rollout flag: ``off`` never evaluates,
    ``shadow`` logs decisions and keeps paginating, ``on`` enforces the stop.
    ``window`` bounds how many trailing result identities the saturation ratio
    is computed over; ``theta`` is the known-ratio threshold; ``min_pages``
    forbids stopping on the first page(s); ``min_trust`` is the registry row
    count below which the trust gate fails and full passes are forced.
    """

    mode: str = "off"
    window: int = 100
    theta: float = 0.9
    min_pages: int = 2
    min_trust: int = 1000

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_early_stop_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding.
        mode = str(self.mode).strip().lower()
        if mode not in ("off", "shadow", "on"):
            raise ValueError("early_stop.mode must be one of: off, shadow, on")
        self.mode = mode

        self.window = int(self.window)
        if self.window <= 0:
            raise ValueError("early_stop.window must be positive")

        self.theta = float(self.theta)
        if not (0.5 <= self.theta <= 1.0):
            raise ValueError("early_stop.theta must be between 0.5 and 1.0")

        self.min_pages = int(self.min_pages)
        if self.min_pages < 1:
            raise ValueError("early_stop.min_pages must be at least 1")

        self.min_trust = int(self.min_trust)
        if self.min_trust < 0:
            raise ValueError("early_stop.min_trust must be non-negative")


@dataclass
class CheckSkipConfig:
    """Inline CheckStage skip configuration (add-key-ledger).

    ``mode`` is the two-position rollout flag: ``off`` never consults the ledger
    for a skip decision, ``on`` suppresses redundant provider calls within the
    per-status freshness window.  ``ttl_hours`` maps a stored status to how long
    it is trusted before the key is re-checked; missing statuses fall back to the
    canonical defaults in :mod:`storage.key_ledger`.
    """

    mode: str = "off"
    ttl_hours: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_check_skip_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding.
        mode = str(self.mode).strip().lower()
        if mode not in ("off", "on"):
            raise ValueError("check_skip.mode must be one of: off, on")
        self.mode = mode

        # Canonical defaults live in storage.key_ledger so the engine and the
        # config never drift; imported lazily to keep config foundational.
        from storage.key_ledger import DEFAULT_TTL_HOURS

        merged: Dict[str, float] = {status: float(hours) for status, hours in DEFAULT_TTL_HOURS.items()}
        for status, hours in (self.ttl_hours or {}).items():
            merged[str(status)] = float(hours)
        for status, hours in merged.items():
            if hours <= 0:
                raise ValueError(f"check_skip.ttl_hours.{status} must be positive")
        self.ttl_hours = merged


@dataclass
class RecheckConfig:
    """Periodic key re-check driver configuration (add-key-ledger).

    ``enabled`` gates the background driver; ``interval_hours`` is the tick
    period and ``batch_size`` bounds how many expired keys are enqueued per tick.
    """

    enabled: bool = False
    interval_hours: float = 6.0
    batch_size: int = 50

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_recheck_config on purpose.
        if not isinstance(self.enabled, bool):
            raise ValueError("recheck.enabled must be a boolean")
        self.interval_hours = float(self.interval_hours)
        if self.interval_hours <= 0:
            raise ValueError("recheck.interval_hours must be positive")
        self.batch_size = int(self.batch_size)
        if self.batch_size <= 0:
            raise ValueError("recheck.batch_size must be positive")


@dataclass
class PrioritizationConfig:
    """Repository priority scoring configuration (add-target-prioritization).

    Weights and thresholds mirror the scoring formula; ``display_top_n`` gates
    the optional StatusManager top-N candidates line (0 = off).
    """

    w1: float = 100.0
    w2: float = 40.0
    w4: float = 30.0
    half_life_days: float = 30.0
    w5: float = 20.0
    threshold_kb: float = 50_000.0
    ramp_kb: float = 500_000.0
    display_top_n: int = 0

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_prioritization_config on purpose.
        # Weights are magnitudes, so zero is valid (S10 sets W1=0); the decay
        # half-life must stay positive to avoid division by zero.
        for name in ("w1", "w2", "w4", "w5"):
            value = float(getattr(self, name))
            if value < 0:
                raise ValueError(f"prioritization.{name} must be non-negative")
            setattr(self, name, value)

        self.half_life_days = float(self.half_life_days)
        if self.half_life_days <= 0:
            raise ValueError("prioritization.half_life_days must be positive")

        self.threshold_kb = float(self.threshold_kb)
        if self.threshold_kb < 0:
            raise ValueError("prioritization.threshold_kb must be non-negative")

        self.ramp_kb = float(self.ramp_kb)
        if self.ramp_kb <= self.threshold_kb:
            raise ValueError("prioritization.ramp_kb must be > threshold_kb")

        self.display_top_n = int(self.display_top_n)
        if self.display_top_n < 0:
            raise ValueError("prioritization.display_top_n must be non-negative")


@dataclass
class AggregationConfig:
    """Shared search-response aggregation configuration (add-search-aggregation).

    ``mode`` is a three-position rollout flag: ``off`` never shares, ``shadow``
    performs every real request and records would-be-hit comparisons without
    serving them, ``on`` serves shared responses.  TTLs are per transport;
    the byte cap bounds the in-memory LRU; ``join_timeout_s`` bounds how long a
    singleflight joiner waits before falling back to its own request.
    """

    mode: str = "off"
    ttl_web_s: float = 120.0
    ttl_api_s: float = 300.0
    max_bytes: int = 64 * 1024 * 1024
    join_timeout_s: float = 60.0

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_aggregation_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding.
        mode = str(self.mode).strip().lower()
        if mode not in ("off", "shadow", "on"):
            raise ValueError("aggregation.mode must be one of: off, shadow, on")
        self.mode = mode

        for name in ("ttl_web_s", "ttl_api_s"):
            value = float(getattr(self, name))
            if not (1 <= value <= 3600):
                raise ValueError(f"aggregation.{name} must be between 1 and 3600")
            setattr(self, name, value)

        self.max_bytes = int(self.max_bytes)
        if self.max_bytes < 1024 * 1024:
            raise ValueError("aggregation.max_bytes must be at least 1 MiB")

        self.join_timeout_s = float(self.join_timeout_s)
        if not (1 <= self.join_timeout_s <= 600):
            raise ValueError("aggregation.join_timeout_s must be between 1 and 600")


@dataclass
class RefineGovernorConfig:
    """Search-work fan-out governor configuration (add-refine-fanout-governor).

    ``mode`` is the project tri-mode rollout flag: ``off`` reproduces the
    pre-change (unbounded) behavior byte-equivalently, ``shadow`` enqueues every
    child while counting/logging would-be refusals, ``on`` enforces.  The caps
    bound refinement recursion depth, per-refine partition width and the total
    number of admitted refined children per process run (roots and pagination
    tasks exempt).  Defaults are grounded in the 2026-09-21 measurements
    (see design D7).
    """

    mode: str = "on"
    max_refine_depth: int = 2
    max_partitions_per_refine: int = 128
    max_search_tasks_per_run: int = 10000

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_refine_governor_config on purpose:
        # this guards direct (non-loader) construction in tests and embedding.
        mode = str(self.mode).strip().lower()
        if mode not in ("off", "shadow", "on"):
            raise ValueError("refine_governor.mode must be one of: off, shadow, on")
        self.mode = mode

        for name in ("max_refine_depth", "max_partitions_per_refine"):
            value = int(getattr(self, name))
            if value <= 0:
                raise ValueError(f"refine_governor.{name} must be positive")
            setattr(self, name, value)

        # The run budget is a cap, so non-positive values fail validation loudly
        # (spec requirement 5).  ``RefineGovernor`` itself stays tolerant of 0 so
        # tests/embedders can construct an already-exhausted budget directly;
        # that state is simply not reachable through configuration.
        self.max_search_tasks_per_run = int(self.max_search_tasks_per_run)
        if self.max_search_tasks_per_run <= 0:
            raise ValueError("refine_governor.max_search_tasks_per_run must be positive")

        if self.max_refine_depth > 5:
            raise ValueError("refine_governor.max_refine_depth must be at most 5")


@dataclass
class TaskQueueConfig:
    """Durable task-queue backend configuration (fix-queue-persistence-under-load).

    ``backend`` selects the queue implementation per stage: ``memory`` keeps the
    historical in-RAM ``queue.Queue`` behavior byte-identically, ``sqlite``
    enables a durable per-stage store.  ``visibility_timeout_s`` bounds how long
    a claimed-but-unacknowledged task stays invisible before the periodic sweep
    returns it to pending; ``max_age_hours`` is the startup/import age gate
    (parity with the legacy snapshot gate).
    """

    backend: str = "memory"
    visibility_timeout_s: float = 300.0
    max_age_hours: float = 24.0

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_task_queue_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding.
        backend = str(self.backend).strip().lower()
        if backend not in ("memory", "sqlite"):
            raise ValueError("queue.backend must be one of: memory, sqlite")
        self.backend = backend

        self.visibility_timeout_s = float(self.visibility_timeout_s)
        if self.visibility_timeout_s <= 0:
            raise ValueError("queue.visibility_timeout_s must be positive")

        self.max_age_hours = float(self.max_age_hours)
        if self.max_age_hours <= 0:
            raise ValueError("queue.max_age_hours must be positive")


@dataclass
class GatherConfig:
    """Gather-stage content transport configuration (fix-gather-transport).

    ``transport`` selects how the gather stage obtains file content:
    ``html`` reproduces the pre-change anonymous rendered-page fetch,
    ``raw`` (the default) addresses the plain-content host derived from the
    discovered blob link, and ``rest`` uses the authenticated REST contents
    endpoint.  ``max_payload_bytes`` bounds a streamed plain-content read
    (design D8); ``max_refusal_wait_s`` bounds any refusal sleep so it can
    never exceed the durable queue visibility window (design D5).
    """

    transport: str = "raw"
    max_payload_bytes: int = 8 * 1024 * 1024
    max_refusal_wait_s: float = 60.0

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_gather_config on purpose: this
        # guards direct (non-loader) construction in tests and embedding.
        transport = str(self.transport).strip().lower()
        if transport not in ("html", "raw", "rest"):
            raise ValueError(f"gather.transport must be one of: html, raw, rest (got: {self.transport!r})")
        self.transport = transport

        self.max_payload_bytes = int(self.max_payload_bytes)
        if self.max_payload_bytes <= 0:
            raise ValueError("gather.max_payload_bytes must be positive")

        self.max_refusal_wait_s = float(self.max_refusal_wait_s)
        if self.max_refusal_wait_s <= 0:
            raise ValueError("gather.max_refusal_wait_s must be positive")


@dataclass
class CredentialLivenessConfig:
    """Credential-liveness / bounded-wait configuration (fix-credential-liveness).

    ``wait_mode`` selects the credential selector behavior: ``bounded`` (the
    default, design D2) computes a deadline before every sleep and raises a
    typed exhaustion when the budget is spent; ``blocking`` restores the
    pre-change unbounded waiter byte-for-byte (config-flip rollback, no code
    removal).  ``max_wait_s`` is that budget; ``early_release`` frees a
    proven-working credential from cooldown immediately (design D4);
    ``emergency_threshold`` is the number of consecutive exhaustion episodes on
    one service that trips the loud emergency state (design D7).
    """

    wait_mode: str = "bounded"
    max_wait_s: float = 60.0
    early_release: bool = True
    emergency_threshold: int = 3

    def __post_init__(self):
        # Mirrors ConfigValidator._validate_credential_liveness_config on
        # purpose: this guards direct (non-loader) construction in tests and
        # embedding.  Loud rejection, GatherConfig precedent.
        wait_mode = str(self.wait_mode).strip().lower()
        if wait_mode not in ("bounded", "blocking"):
            raise ValueError(f"credential_liveness.wait_mode must be one of: bounded, blocking (got: {self.wait_mode!r})")
        self.wait_mode = wait_mode

        self.max_wait_s = float(self.max_wait_s)
        if self.max_wait_s <= 0:
            raise ValueError("credential_liveness.max_wait_s must be positive")

        self.early_release = bool(self.early_release)

        self.emergency_threshold = int(self.emergency_threshold)
        if self.emergency_threshold <= 0:
            raise ValueError("credential_liveness.emergency_threshold must be positive")


@dataclass
class ApiConfig:
    """API configuration for a provider"""
    base_url: str = ""
    completion_path: str = ""
    model_path: str = ""
    default_model: str = ""
    auth_key: str = "Authorization"
    extra_headers: Dict[str, str] = field(default_factory=dict)
    api_version: str = ""
    timeout: int = 30
    retries: int = 3


@dataclass
class StageConfig:
    """Pipeline stage configuration for individual tasks"""

    search: bool = True
    gather: bool = True
    check: bool = True
    inspect: bool = True

    def validate(self) -> None:
        """Validate stage dependencies"""
        if not self.check and self.inspect:
            raise ValueError("inspect stage requires check stage to be enabled")


@dataclass
class StorageConfig:
    """Result storage grouping for a provider task"""

    directory: str = ""
    plan: str = ""


@dataclass
class TaskConfig:
    """Configuration for a single provider task"""

    name: str = ""
    enabled: bool = True
    provider_type: str = ""
    use_api: bool = False
    stages: StageConfig = field(default_factory=StageConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    extras: Dict[str, Any] = field(default_factory=dict)
    api: ApiConfig = field(default_factory=ApiConfig)
    patterns: Patterns = field(default_factory=Patterns)
    conditions: List[Condition] = field(default_factory=list)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


@dataclass
class WorkerManagerConfig:
    """Worker manager configuration for dynamic thread management"""

    # Enable/disable worker manager (default: disabled)
    enabled: bool = False
    min_workers: int = 1
    max_workers: int = 10
    target_queue_size: int = 100
    adjustment_interval: float = 5.0
    scale_up_threshold: float = 0.8
    scale_down_threshold: float = 0.2

    # Enable/disable worker adjustment recommendation logging
    log_recommendations: bool = True

    def __post_init__(self):
        """Validate worker manager configuration"""
        if self.min_workers < 1:
            raise ValueError("min_workers must be at least 1")
        if self.max_workers < self.min_workers:
            raise ValueError("max_workers must be >= min_workers")
        if self.target_queue_size < 0:
            raise ValueError("target_queue_size must be non-negative")
        if self.adjustment_interval <= 0:
            raise ValueError("adjustment_interval must be positive")
        if not (0 < self.scale_up_threshold < 1):
            raise ValueError("scale_up_threshold must be between 0 and 1")
        if not (0 < self.scale_down_threshold < 1):
            raise ValueError("scale_down_threshold must be between 0 and 1")
        if self.scale_down_threshold >= self.scale_up_threshold:
            raise ValueError("scale_down_threshold must be < scale_up_threshold")

    def is_scale_up_needed(self, queue_ratio: float) -> bool:
        """Check if scale up is needed based on queue ratio"""
        return queue_ratio > self.scale_up_threshold

    def is_scale_down_needed(self, queue_ratio: float) -> bool:
        """Check if scale down is needed based on queue ratio"""
        return queue_ratio < self.scale_down_threshold

    def calculate_target_workers(self, current_queue_size: int, current_workers: int) -> int:
        """Calculate target number of workers based on current metrics"""
        if current_queue_size == 0:
            return max(self.min_workers, current_workers - 1)

        queue_ratio = current_queue_size / max(self.target_queue_size, 1)

        if queue_ratio > self.scale_up_threshold:
            target = min(self.max_workers, current_workers + 1)
        elif queue_ratio < self.scale_down_threshold:
            target = max(self.min_workers, current_workers - 1)
        else:
            target = current_workers

        return target


@dataclass
class Config:
    """Main configuration container"""

    global_config: GlobalConfig = field(default_factory=GlobalConfig)
    pipeline: PipelineConfig = field(default_factory=PipelineConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    persistence: PersistenceConfig = field(default_factory=PersistenceConfig)
    worker: WorkerManagerConfig = field(default_factory=WorkerManagerConfig)
    registry: RegistryConfig = field(default_factory=RegistryConfig)
    skip: SkipConfig = field(default_factory=SkipConfig)
    enrichment: EnrichmentConfig = field(default_factory=EnrichmentConfig)
    early_stop: EarlyStopConfig = field(default_factory=EarlyStopConfig)
    check_skip: CheckSkipConfig = field(default_factory=CheckSkipConfig)
    recheck: RecheckConfig = field(default_factory=RecheckConfig)
    prioritization: PrioritizationConfig = field(default_factory=PrioritizationConfig)
    aggregation: AggregationConfig = field(default_factory=AggregationConfig)
    refine_governor: RefineGovernorConfig = field(default_factory=RefineGovernorConfig)
    queue: TaskQueueConfig = field(default_factory=TaskQueueConfig)
    gather: GatherConfig = field(default_factory=GatherConfig)
    credential_liveness: CredentialLivenessConfig = field(default_factory=CredentialLivenessConfig)
    ratelimits: Dict[str, RateLimitConfig] = field(default_factory=dict)
    tasks: List[TaskConfig] = field(default_factory=list)

    def __post_init__(self):
        """Set default rate limits if none provided"""
        if not self.ratelimits:
            self.ratelimits = {
                "github_api": RateLimitConfig(base_rate=0.15, burst_limit=3, adaptive=True),
                "github_web": RateLimitConfig(base_rate=0.5, burst_limit=2, adaptive=True),
                "github_raw": RateLimitConfig(base_rate=2.0, burst_limit=4, adaptive=True),
            }

    def to_dict(self) -> Dict[str, Any]:
        """Convert Config object to dictionary

        Returns:
            Dict[str, Any]: Configuration as dictionary with proper structure
        """
        return {
            "global": self._dataclass_to_dict(self.global_config),
            "pipeline": self._dataclass_to_dict(self.pipeline),
            "monitoring": self._dataclass_to_dict(self.monitoring),
            "display": self._dataclass_to_dict(self.display),
            "persistence": self._dataclass_to_dict(self.persistence),
            "worker": self._dataclass_to_dict(self.worker),
            "registry": self._dataclass_to_dict(self.registry),
            "skip": self._dataclass_to_dict(self.skip),
            "enrichment": self._dataclass_to_dict(self.enrichment),
            "early_stop": self._dataclass_to_dict(self.early_stop),
            "check_skip": self._dataclass_to_dict(self.check_skip),
            "recheck": self._dataclass_to_dict(self.recheck),
            "prioritization": self._dataclass_to_dict(self.prioritization),
            "aggregation": self._dataclass_to_dict(self.aggregation),
            "refine_governor": self._dataclass_to_dict(self.refine_governor),
            "queue": self._dataclass_to_dict(self.queue),
            "gather": self._dataclass_to_dict(self.gather),
            "credential_liveness": self._dataclass_to_dict(self.credential_liveness),
            "ratelimits": {k: self._dataclass_to_dict(v) for k, v in self.ratelimits.items()},
            "tasks": [self._dataclass_to_dict(task) for task in self.tasks],
        }

    def _dataclass_to_dict(self, obj: Any) -> Any:
        """Convert dataclass object to dictionary recursively

        Args:
            obj: Object to convert (dataclass, dict, list, or primitive)

        Returns:
            Any: Converted object
        """
        if hasattr(obj, "__dataclass_fields__"):
            # Handle dataclass objects
            result = {}
            for field_name in obj.__dataclass_fields__.keys():
                value = getattr(obj, field_name)
                result[field_name] = self._dataclass_to_dict(value)
            return result
        elif isinstance(obj, dict):
            # Handle dictionaries
            return {k: self._dataclass_to_dict(v) for k, v in obj.items()}
        elif isinstance(obj, (list, tuple)):
            # Handle lists and tuples
            return [self._dataclass_to_dict(item) for item in obj]
        elif hasattr(obj, "value"):
            # Handle enums
            return obj.value
        else:
            # Handle primitive types
            return obj
