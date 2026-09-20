#!/usr/bin/env python3

"""
Dynamic pipeline system for asynchronous multi-provider task processing.
Implements producer-consumer pattern with configurable worker threads and dynamic stage management.
"""

import time
from typing import Dict, List, Optional

from config.schemas import Config
from core.auth import configure_auth, get_auth_provider
from core.enums import PipelineStage, SystemState
from core.metrics import DateFillMetrics, PipelineStatus
from core.models import ProviderTask
from core.types import IPipelineStats, IProvider
from search import client
from search.aggregation import get_aggregation_metrics
from stage.base import BasePipelineStage, StageOutput, StageResources, StageUtils
from stage.registry import StageRegistryMixin
from stage.resolver import DependencyResolver
from storage.persistence import MultiResultManager
from storage.early_stop import EarlyStopEngine
from storage.gather_skip import GatherSkipEngine
from storage.key_ledger import KeyLedger
from storage.registry import Registry, config_digest as build_config_digest, init_registry
from storage.priority import top_candidates
from storage.repo_meta import RepoMetaEnricher, RepoMetaStore, tokens_cooling_down
from tools.coordinator import get_session, get_token, get_user_agent
from tools.logger import get_logger
from tools.ratelimit import RateLimiter

from .base import LifecycleManager
from .queue import QueueManager
from .recheck import RecheckManager, ShardKeyResolver

logger = get_logger("manager")


class Pipeline(IPipelineStats, StageRegistryMixin, LifecycleManager):
    """Dynamic pipeline coordinator with registry-based stage management

    Inherits from PipelineBase to provide type-safe statistics interface,
    from StageRegistryMixin for stage management capabilities,
    and from LifecycleManager for lifecycle management.
    """

    def __init__(self, config: Config, providers: Dict[str, IProvider]):
        # Initialize base classes
        LifecycleManager.__init__(self, "Pipeline")

        self.config = config
        self.providers: Dict[str, IProvider] = providers

        # Configure authentication service
        configure_auth(session_provider=get_session, token_provider=get_token, user_agent_provider=get_user_agent)

        # Create shared components
        self.result_manager = MultiResultManager(
            workspace=config.global_config.workspace,
            providers=providers,
            batch_size=config.persistence.batch_size,
            save_interval=config.persistence.save_interval,
            simple=config.persistence.simple,
            shutdown_timeout=float(config.persistence.shutdown_timeout),
        )

        self.rate_limiter = RateLimiter(config.ratelimits)

        # Write-only link registry (no-op singleton when disabled).
        # NOTE: named `link_registry` to avoid clashing with the inherited
        # `registry` property that exposes the stage registry.
        self.link_registry: Registry = init_registry(
            config.global_config.workspace,
            config=config.registry,
            prioritization=config.prioritization,
        )

        # Registry-driven gather-skip decision engine (off by default; never
        # reads the registry unless skip_known is shadow/on).
        self.gather_skip = GatherSkipEngine(
            workspace=config.global_config.workspace,
            mode=config.skip.skip_known,
            ttl_hours=config.skip.gather_ttl_hours,
            registry_path=self.link_registry.path,
        )

        # Frontier-based API pagination early-stop detector (off by default;
        # API transport only).  "Known" reuses gather-skip semantics and the
        # kill-switch is the existing registry degraded flag.
        self.early_stop = EarlyStopEngine(
            workspace=config.global_config.workspace,
            mode=config.early_stop.mode,
            window=config.early_stop.window,
            theta=config.early_stop.theta,
            min_pages=config.early_stop.min_pages,
            min_trust=config.early_stop.min_trust,
            ttl_hours=config.skip.gather_ttl_hours,
            registry_path=self.link_registry.path,
            degraded_probe=lambda: self.link_registry.degraded,
            # Share the gather-skip engine so its registry read errors (the
            # spec kill-switch) also mute early-stop for the run.
            skip_engine=self.gather_skip,
        )

        # Date-extraction fill-rate observability (per run, fail-open).
        self.date_metrics = DateFillMetrics()

        # Key-ledger skip engine (off by default; never reads the registry
        # unless check_skip.mode is on) plus its periodic re-check driver.
        self.key_ledger = KeyLedger(
            workspace=config.global_config.workspace,
            mode=config.check_skip.mode,
            ttl_hours=config.check_skip.ttl_hours,
            registry_path=self.link_registry.path,
            degraded_cb=self.link_registry.mark_degraded,
        )
        self.recheck = RecheckManager(
            workspace=config.global_config.workspace,
            registry_path=self.link_registry.path,
            ttl_hours=config.check_skip.ttl_hours,
            enabled=bool(config.recheck.enabled and config.registry.enabled),
            interval_hours=config.recheck.interval_hours,
            batch_size=config.recheck.batch_size,
            enqueue=self._enqueue_recheck_task,
            resolve=ShardKeyResolver(config.global_config.workspace),
        )

        # Configure the shared HTTP opener before any stage starts making network requests
        client.set_proxy(config.global_config.proxy)

        # Initialize GitHub client rate limiter
        client.init_github_client(config.ratelimits)

        # Shared search-response aggregation (off by default; config-only rollback)
        self._configure_aggregation(config)

        # Repo-metadata enrichment (off by default).  The cache is the registry
        # and the enricher rides the shared github_api client/buckets.
        self.repo_meta_store = RepoMetaStore(
            workspace=config.global_config.workspace,
            ttl_hours=config.enrichment.ttl_hours,
            registry=self.link_registry,
            registry_path=self.link_registry.path,
        )
        self.enrichment = RepoMetaEnricher(
            self.repo_meta_store,
            auth=get_auth_provider(),
            client=client.get_github_client(),
            enabled=config.enrichment.enabled,
            ttl_hours=config.enrichment.ttl_hours,
            cooling_probe=tokens_cooling_down,
        )

        self.queue_manager = QueueManager(
            workspace=config.global_config.workspace,
            save_interval=config.persistence.queue_interval,
            shutdown_timeout=float(config.persistence.shutdown_timeout),
        )

        # Start periodic snapshots for results
        if not config.persistence.simple:
            try:
                self.result_manager.start_periodic_snapshots(config.persistence.snapshot_interval)
            except Exception as e:
                logger.error(f"Failed to start periodic snapshots: {e}")
        else:
            logger.debug("Skipping periodic snapshots in simple mode")

        # Store task configs for stage checking (must be before _create_stages)
        self.task_configs = {task.name: task for task in config.tasks if task.enabled}

        # Dynamic stage management
        self.stages: Dict[str, BasePipelineStage] = {}
        self.resolver = DependencyResolver(self.registry)

        # Create pipeline stages dynamically
        self._create_stages()

        # Cache dependency resolution results
        self._order_cache = None
        self._init_order_cache()

        # Statistics and completion tracking
        self.start_time = time.time()
        self.initial_tasks_count = 0

        logger.info(f"Initialized dynamic pipeline with {len(self.stages)} stages: {list(self.stages.keys())}")

    def _configure_aggregation(self, config: Config) -> None:
        """Install the process-wide shared-response aggregator from config.

        Read once at wiring time (mode flips require a restart, consistent
        with the other tri-mode features).  A construction failure is loud but
        non-fatal: the run falls back to ``off`` (every fetch real).
        """
        from search.aggregation import SearchAggregator, configure_aggregator

        agg_cfg = config.aggregation
        try:
            configure_aggregator(
                SearchAggregator(
                    mode=agg_cfg.mode,
                    ttl_web_s=agg_cfg.ttl_web_s,
                    ttl_api_s=agg_cfg.ttl_api_s,
                    max_bytes=agg_cfg.max_bytes,
                    join_timeout_s=agg_cfg.join_timeout_s,
                    workspace=config.global_config.workspace,
                )
            )
            logger.info(
                f"Aggregation configured: mode={agg_cfg.mode} "
                f"ttl_web={agg_cfg.ttl_web_s}s ttl_api={agg_cfg.ttl_api_s}s "
                f"max_bytes={agg_cfg.max_bytes} join_timeout={agg_cfg.join_timeout_s}s"
            )
        except Exception as e:
            logger.error(f"Failed to configure aggregation, falling back to off: {e}")
            configure_aggregator(None)

    def _aggregate_stages(self) -> List[str]:
        """Aggregate stage requirements from all enabled tasks"""
        requested = set()

        # Collect all stages requested by enabled tasks
        for task in self.config.tasks:
            if task.enabled:
                enabled = StageUtils.get_enabled(task)
                requested.update(enabled)

                logger.debug(f"  {task.name}: [{', '.join(enabled)}]")

        result = list(requested)
        logger.info(f"Aggregated stages to create: {result}")
        return result

    def _create_stages(self) -> None:
        """Create pipeline stages dynamically with hybrid architecture"""

        # Get requested stages from configuration
        requested_stages = self._aggregate_stages()

        if not requested_stages:
            logger.warning("No stages requested, pipeline will be empty")
            return

        # Resolve stage creation order
        try:
            ordered_stages = self.resolver.resolve_order(requested_stages)
        except Exception as e:
            logger.error(f"Failed to resolve stage dependencies: {e}")
            raise

        # Create shared resources for dependency injection
        resources = StageResources(
            limiter=self.rate_limiter,
            providers=self.providers,
            config=self.config,
            task_configs=self.task_configs,
            auth=get_auth_provider(),
            registry=self.link_registry,
            date_metrics=self.date_metrics,
            gather_skip=self.gather_skip,
            enrichment=self.enrichment,
            early_stop=self.early_stop,
            key_ledger=self.key_ledger,
        )

        # Create stages in dependency order
        thread_config = self.config.pipeline.threads
        queue_config = self.config.pipeline.queue_sizes

        for name in ordered_stages:
            definition = self.get_stage_def(name)
            if not definition:
                logger.error(f"Stage definition not found: {name}")
                continue

            try:
                # Create stage instance with hybrid architecture
                stage = definition.stage_class(
                    resources=resources,
                    handler=self._handle_stage_output,
                    thread_count=max(thread_config.get(name, 1), 1),
                    queue_size=max(queue_config.get(name, 1000), 1),
                    max_retries=self.config.global_config.max_retries_requeued,
                )

                self.stages[name] = stage

            except Exception as e:
                logger.error(f"Failed to create stage {name}: {e}")
                raise

    def _on_start(self) -> None:
        """Start all pipeline stages"""
        # Open the write-only registry before stages begin producing
        self.link_registry.start()

        if not self.stages:
            logger.warning("No stages to start")
            return

        # Start stages in dependency order
        ordered_stages = self.get_order()

        for stage_name in ordered_stages:
            stage = self.stages.get(stage_name)
            if stage:
                stage.start()

        if self.recheck.enabled:
            self.recheck.start()

        logger.info(f"Started {len(self.stages)} pipeline stages")

    def _on_stop(self) -> None:
        """Stop all pipeline stages"""
        # Stop the re-check driver first so it cannot enqueue into draining stages.
        try:
            self.recheck.stop()
        except Exception as e:  # pragma: no cover - defensive
            logger.debug(f"Failed to stop recheck manager: {e}")

        if not self.stages:
            return

        # Stop stages in reverse dependency order
        ordered_stages = self.get_order()
        stage_timeout = 30.0 / len(self.stages) if self.stages else 30.0

        for stage_name in reversed(ordered_stages):
            stage = self.stages.get(stage_name)
            if stage:
                stage.stop(stage_timeout)

        # Drain and close the registry before result managers flush
        self.link_registry.stop()
        self.gather_skip.close()
        self.enrichment.close()
        self.early_stop.close()
        self.key_ledger.close()
        self.recheck.close()

        # Stop managers
        self.queue_manager.stop()
        self.result_manager.stop_all()

        logger.info("Stopped all pipeline stages")

    def start_registry_run(self) -> None:
        """Journal the start of a run (write-only, fail-open)."""
        try:
            if self.link_registry.available:
                self.link_registry.start_run(config_digest=build_config_digest(self.config))
                self.gather_skip.run_id = self.link_registry.run_id
                self.early_stop.run_id = self.link_registry.run_id
        except Exception as e:
            logger.warning(f"Failed to journal registry run start: {e}")
            self.link_registry.mark_degraded()

    def finish_registry_run(self) -> None:
        """Journal the graceful finish of a run (write-only, fail-open)."""
        try:
            self.link_registry.finish_run()
        except Exception as e:
            logger.warning(f"Failed to journal registry run finish: {e}")

    def is_finished(self) -> bool:
        """Check if pipeline is finished and manage stage states"""
        if not self.stages:
            return True

        ordered_stages = self.get_order()
        all_finished = True

        # Check each stage and manage accepting state
        for stage_name in ordered_stages:
            stage = self.stages.get(stage_name)
            if not stage:
                continue

            # Stop accepting if stage can finish
            if stage.accepting and self._can_stage_stop_accepting(stage_name):
                stage.stop_accepting()
                logger.info(f"[{stage_name}] stopped accepting new tasks")

            # Check if stage is finished
            if not stage.is_finished():
                all_finished = False

        return all_finished

    def get_all_stats(self) -> PipelineStatus:
        """Get statistics for all stages"""
        return self._get_pipeline_status()

    def get_dynamic_stats(self) -> PipelineStatus:
        """Get dynamic statistics for all stages"""
        return self._get_pipeline_status()

    def _get_pipeline_status(self) -> PipelineStatus:
        """Get pipeline status as PipelineStatus object"""
        stage_status = {}

        # Collect stats from all active stages
        for stage_name, stage in self.stages.items():
            stage_status[stage_name] = stage.get_stats()

        pipeline_status = PipelineStatus(
            state=SystemState.RUNNING if self.stages else SystemState.STOPPED,
            active=len([s for s in self.stages.values() if s.running]),
            total=len(self.stages),
            stages=stage_status,
            runtime=time.time() - self.start_time,
            date_metrics=self.date_metrics.to_stats(),
            skip_metrics=self.gather_skip.to_stats(),
            enrichment_metrics=self.enrichment.to_stats(),
            early_stop_metrics=self.early_stop.to_stats(),
            key_ledger_metrics=self.key_ledger.to_stats(),
            recheck_metrics=self.recheck.to_stats(),
            prioritization_metrics=self._prioritization_metrics(),
            aggregation_metrics=get_aggregation_metrics(),
        )

        return pipeline_status

    def _prioritization_metrics(self) -> Dict[str, object]:
        """Top-N candidate line data (empty unless display_top_n > 0)."""
        top_n = int(getattr(self.config.prioritization, "display_top_n", 0) or 0)
        if top_n <= 0 or not self.link_registry.available:
            return {}
        try:
            return {"display_top_n": top_n, "candidates": top_candidates(self.link_registry.path, top_n)}
        except Exception as e:  # pragma: no cover - cosmetic best-effort
            logger.debug(f"Failed to build prioritization metrics: {e}")
            return {}

    def add_initial_tasks(self, initial_tasks: List[ProviderTask]) -> None:
        """Add initial search tasks to pipeline"""
        self.initial_tasks_count = len(initial_tasks)

        search_stage = self.stages.get(PipelineStage.SEARCH.value)
        if search_stage:
            for task in initial_tasks:
                search_stage.put_task(task)
        else:
            logger.warning("Search stage not created, cannot add initial tasks")

        logger.info(f"Added {len(initial_tasks)} initial tasks to pipeline")

    def get_stage(self, name: str) -> Optional[BasePipelineStage]:
        """Get stage by name"""
        return self.stages.get(name)

    def _enqueue_recheck_task(self, task: ProviderTask) -> bool:
        """Route a re-check task through the normal CheckStage queue.

        Uses the standard stage entry point so provider rate limits and token
        buckets apply exactly as for pipeline-originated checks (design D5).
        Returns False when the task was refused (stage disabled/stopped or
        deduped), so the driver's counters stay honest.
        """
        config = self.task_configs.get(task.provider)
        if not config or not StageUtils.check(config, PipelineStage.CHECK.value):
            logger.debug(f"[recheck] check stage disabled for provider {task.provider}, dropping task")
            return False
        stage = self.stages.get(PipelineStage.CHECK.value)
        if stage is None:
            logger.debug(f"[recheck] check stage not created, dropping task for {task.provider}")
            return False
        return bool(stage.put_task(task))

    def _init_order_cache(self) -> None:
        """Initialize and cache stage order"""
        if self.stages:
            self._order_cache = self.resolver.resolve_order(list(self.stages.keys()))
            logger.debug(f"Cached stage order: {self._order_cache}")

    def get_order(self) -> List[str]:
        """Get cached stage order"""
        if self._order_cache is None:
            self._init_order_cache()
        return self._order_cache.copy() if self._order_cache else []

    def _handle_stage_output(self, output: StageOutput) -> None:
        """Handle pure functional stage output - core orchestration logic"""

        # Save results
        for provider, result_type, data in output.results:
            self.result_manager.add_result(provider, result_type, data)

        # Save links
        for provider, links in output.links:
            self.result_manager.add_links(provider, links)

        # Save models
        for provider, key, models in output.models:
            self.result_manager.add_models(provider, key, models)

        # Forward date metadata to the registry writer (non-regressing merge).
        for provider, mapping in output.link_metadata:
            try:
                self.link_registry.record_metadata(mapping)
            except Exception as e:  # pragma: no cover - defensive
                logger.debug(f"Registry metadata hook failed for {provider}: {e}")

        # Route new tasks
        for task, target_stage in output.new_tasks:
            config = self.task_configs.get(task.provider)
            if config and StageUtils.check(config, target_stage):
                stage = self.stages.get(target_stage)
                if stage:
                    stage.put_task(task)
                else:
                    logger.warning(f"Target stage {target_stage} not found for task {task.provider}")
            else:
                logger.debug(f"Stage {target_stage} disabled for {task.provider}, skipping task")

    def _can_stage_stop_accepting(self, stage_name: str) -> bool:
        """Check if a stage can stop accepting new tasks based on precise conditions"""
        stage = self.stages.get(stage_name)
        if not stage:
            return False

        # Stage can stop accepting tasks ONLY when:
        # 1. Own task queue is empty
        # 2. No workers are actively processing tasks
        # 3. All upstream producers are finished (or no upstream producers exist)

        # Check condition 1: Own task queue is empty
        if not stage.queue.empty():
            return False

        # Check condition 2: No workers are actively processing tasks
        if stage.active_workers > 0:
            return False

        # Check condition 3: All upstream producers are finished
        # Get all stages that can potentially send tasks to this stage
        upstream_stages = []
        for other_stage_name in self.stages.keys():
            definition = self.get_stage_def(other_stage_name)
            if definition and stage_name in definition.produces_for:
                upstream_stages.append(other_stage_name)

        # If no upstream stages, consider upstream as finished
        if not upstream_stages:
            return True

        # Check if all upstream stages are finished
        for upstream_name in upstream_stages:
            upstream_stage = self.stages.get(upstream_name)
            if upstream_stage:
                # Upstream stage must be finished (queue empty + no active workers)
                if not upstream_stage.queue.empty():
                    return False
                if upstream_stage.active_workers > 0:
                    return False
                # Also check if upstream is still accepting (could generate more tasks)
                if upstream_stage.accepting:
                    return False

        return True
