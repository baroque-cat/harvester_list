#!/usr/bin/env python3

"""
Task Recovery Strategy - Enhanced Task Recovery Management

This module provides enhanced task recovery functionality using type-safe
stage management and configuration-driven approach.
"""

from typing import Any, Dict, List, Optional, Set

from core.enums import PipelineStage
from core.models import AllRecoveredTasks, ProviderTask, RecoveredTasks, Service
from core.types import IProvider
from stage.base import StageUtils
from stage.factory import TaskFactory
from storage.registry import Registry, patterns_hash, url_hash
from tools.logger import get_logger

from .pipeline import Pipeline

logger = get_logger("manager")

# Meta key for the recovery continuation marker (slice 3a.3, D-03.4).
_RECOVERY_CURSOR_META_KEY = "recovery_cursor"


class TaskRecoveryManager:
    """Enhanced task recovery strategy using type-safe stage management"""

    def __init__(self, pipeline: "Pipeline", providers: Dict[str, IProvider]):
        """Initialize recovery strategy

        Args:
            pipeline: Pipeline instance
            providers: Dictionary of provider instances
        """
        self.pipeline = pipeline
        self.providers = providers

    def recover_queue_tasks(self, queue_tasks: Dict[str, List[ProviderTask]]) -> None:
        """Recover queue tasks using stage registry

        Args:
            queue_tasks: Dictionary mapping stage names to task lists
        """
        if not queue_tasks:
            return

        logger.info("Starting queue task recovery")

        for stage_name, tasks in queue_tasks.items():
            if not tasks:
                continue

            # Validate stage name using enum
            stage_enum = self._get_stage_enum(stage_name)
            if not stage_enum:
                logger.warning(f"Unknown stage name: {stage_name}")
                continue

            # Get stage instance from pipeline
            stage = self.pipeline.get_stage(stage_enum.value)
            if not stage:
                logger.warning(f"Stage not found: {stage_name}")
                continue

            # Recover tasks to stage
            for task in tasks:
                try:
                    stage.put_task(task)
                    logger.debug(f"Recovered task to {stage_name}: {task}")
                except Exception as e:
                    logger.error(f"Failed to recover task to {stage_name}: {e}")

        logger.info(f"Queue task recovery completed for {len(queue_tasks)} stages")

    def recover_result_tasks(self, result_tasks: AllRecoveredTasks) -> None:
        """Recover result tasks with stage enum validation

        Args:
            result_tasks: All recovered tasks data
        """
        if not result_tasks or not result_tasks.providers:
            return

        logger.info("Starting result task recovery")

        for provider_name, provider_tasks in result_tasks.providers.items():
            if provider_name not in self.providers:
                logger.warning(f"Provider not found: {provider_name}")
                continue

            self._recover_provider_tasks(provider_name, provider_tasks)

        logger.info(f"Result task recovery completed for {len(result_tasks.providers)} providers")

    def _recover_provider_tasks(self, name: str, tasks: RecoveredTasks) -> None:
        """Recover tasks for specific provider

        Args:
            name: Name of the provider
            tasks: Recovered tasks for the provider
        """
        config = self.pipeline.task_configs.get(name)
        if not config:
            logger.warning(f"No config found for provider: {name}")
            return

        # Recover check tasks (Service objects can be used directly)
        if StageUtils.check(config, PipelineStage.CHECK):
            self._recover_check_tasks(name, tasks.check, tasks.invalid)

        # Recover acquisition tasks (URLs need to be converted to AcquisitionTask objects)
        if StageUtils.check(config, PipelineStage.GATHER):
            urls, source_label = self._resolve_acquisition_urls(name, tasks.acquisition)
            self._recover_acquisition_tasks(name, urls, source_label=source_label)

    def _resolve_acquisition_urls(self, name: str, shard_urls: List[str]) -> tuple:
        """Resolve the acquisition URL list based on the configured recovery source.

        When ``recovery.source`` is ``shards`` (default), returns ``(shard_urls, "shard")``
        unchanged (byte-identical legacy path).  When ``registry`` or ``auto``
        with a live registry, streams URLs from the registry reader with
        per-boot cap and continuation-marker logic.  When ``auto`` with the
        registry legitimately disabled, falls back to shards.  When the
        registry is enabled but dead, propagates ``RegistryUnavailableError``
        (fail-CLOSED, Д14 — no shard-fallback).

        Args:
            name: Provider name (used for logging)
            shard_urls: Legacy shard-aggregated URL list

        Returns:
            tuple: (List[str], str) — resolved URL list and source label
        """
        recovery_cfg = getattr(getattr(self.pipeline, "config", None), "recovery", None)
        source = getattr(recovery_cfg, "source", "shards") if recovery_cfg else "shards"
        per_boot_cap = getattr(recovery_cfg, "per_boot_cap", 0) if recovery_cfg else 0

        if source == "shards":
            return shard_urls, "shard"

        # source is "registry" or "auto" — need the registry.
        # D1 fix: Pipeline stores registry as link_registry, not registry.
        registry = getattr(self.pipeline, "link_registry", None)
        if registry is None:
            # No registry attached — fall back to shards silently for auto,
            # but crash for explicit registry mode.
            if source == "registry":
                from core.exceptions import RegistryUnavailableError
                raise RegistryUnavailableError(
                    "recovery.source=registry but no registry is attached to pipeline"
                )
            return shard_urls, "shard"

        registry_enabled = getattr(registry, "enabled", False)
        registry_available = getattr(registry, "available", False)

        if source == "auto" and not registry_enabled:
            # Registry legitimately disabled — shards path (neutral).
            return shard_urls, "shard"

        if source == "registry" and not registry_enabled:
            from core.exceptions import RegistryUnavailableError
            raise RegistryUnavailableError(
                "recovery.source=registry but registry is not enabled"
            )

        # Registry is enabled — use the SQL reader.
        # check_alive() will raise if the DB died.
        registry.check_alive()

        # Read continuation marker from meta (per-provider key, D3 fix).
        meta_key = f"{_RECOVERY_CURSOR_META_KEY}:{name}"
        cursor_ts = None
        cursor_hash = None
        marker_raw = registry.get_meta(meta_key)
        if marker_raw:
            # Composite marker format: "{ts}|{hash}" (D2 fix).
            # Legacy format (ts-only) is supported with WARNING.
            if "|" in marker_raw:
                parts = marker_raw.split("|", 1)
                try:
                    cursor_ts = float(parts[0])
                    cursor_hash = parts[1] if len(parts) > 1 else None
                except (ValueError, TypeError):
                    logger.warning(
                        f"[recovery] unparseable composite marker for {name}: {marker_raw!r}; "
                        f"resetting to newest"
                    )
                    cursor_ts = None
                    cursor_hash = None
            else:
                # Legacy ts-only marker (backward compat).
                try:
                    cursor_ts = float(marker_raw)
                    logger.warning(
                        f"[recovery] legacy ts-only marker for {name}: {marker_raw!r}; "
                        f"potential data loss at boundary; consider manual reset"
                    )
                except (ValueError, TypeError):
                    logger.warning(
                        f"[recovery] unparseable legacy marker for {name}: {marker_raw!r}; "
                        f"resetting to newest"
                    )
                    cursor_ts = None

        # Stream URLs from the registry with optional cap and provider filter.
        seen_hashes: Set[str] = set()
        result_urls: List[str] = []
        last_ts = None
        last_hash = None
        total_available = 0
        truncated = False

        for url_val, uh, ts_val in registry.fetch_recoverable_urls(
            limit=per_boot_cap if per_boot_cap > 0 else 0,
            before_ts=cursor_ts,
            before_hash=cursor_hash,
            provider=name,
        ):
            total_available += 1
            if uh in seen_hashes:
                continue
            seen_hashes.add(uh)
            result_urls.append(url_val)
            last_ts = ts_val
            last_hash = uh

            if per_boot_cap > 0 and len(result_urls) >= per_boot_cap:
                truncated = True
                break

        # Handle continuation marker.
        if truncated and last_ts is not None and last_hash is not None:
            # Write composite cursor so next boot continues below this point (D2 fix).
            registry.set_meta(meta_key, f"{last_ts}|{last_hash}")
            logger.info(
                f"[recovery_truncated] provider={name} took={len(result_urls)} "
                f"cap={per_boot_cap} total_seen={total_available} "
                f"cursor_ts={last_ts} cursor_hash={last_hash[:16]}..."
            )
        else:
            # Full pass completed — remove the marker.
            registry.delete_meta(meta_key)

        # Publication: loud INFO with the canonical token.
        logger.info(
            f"recovered_from_registry={len(result_urls)} provider={name} "
            f"total_available={total_available}"
        )

        return result_urls, "registry"

    def _recover_check_tasks(self, name: str, check_tasks: List[Service], invalid_keys: Set[Service]) -> None:
        """Recover check tasks for provider

        Args:
            name: Name of the provider
            check_tasks: List of services to check
            invalid_keys: Set of invalid services
        """
        stage_instance = self.pipeline.get_stage(PipelineStage.CHECK.value)
        if not stage_instance:
            logger.warning(f"Check stage not found for {name}")
            return

        provider = self.providers.get(name)
        if not provider:
            logger.warning(f"Provider not found: {name}")
            return

        recovered_count = 0

        # Recover valid check tasks
        for service in check_tasks:
            if not service or (invalid_keys and service in invalid_keys):
                continue

            try:
                check_task = TaskFactory.create_check_task(name, service)
                stage_instance.put_task(check_task)
                recovered_count += 1

            except Exception as e:
                logger.error(f"Failed to create check task for {name}: {e}")

        if recovered_count > 0:
            logger.info(f"Recovered {recovered_count} check tasks for {name}")

    def _recover_acquisition_tasks(self, name: str, acquisition_tasks: List[str], source_label: str = "shard") -> None:
        """Recover acquisition tasks for provider

        P26: apply the same creation-time policy stack the search stage uses
        (gather-skip decision first, then cross-provider share claim) BEFORE
        put_task, so a restart stops re-enqueueing the entire cumulative shard
        history (measured 12 268 tasks on one restart, unbounded growth;
        plan.md II.19 P26 / I.23).  Both layers ride their EXISTING flags
        (``skip.skip_known``, ``gather.share_fetch``); with both off the replay
        is byte-identical to the pre-P26 behaviour.  Every layer fails open in
        the enqueue direction.  No repo-meta refresh and no dating hooks run
        here (D4): startup consumes STORED registry state only — refreshing
        thousands of URLs would hammer the github_api basket before workers
        start, and push evidence is NULL anyway while enrichment is off.

        Args:
            name: Name of the provider
            acquisition_tasks: List of URLs to acquire
            source_label: Origin label for the log line ("shard" or "registry")
        """
        stage_instance = self.pipeline.get_stage(PipelineStage.GATHER.value)
        if not stage_instance:
            logger.warning(f"Acquisition stage not found for {name}")
            return

        provider = self.providers.get(name)
        if not provider:
            logger.warning(f"Provider not found: {name}")
            return

        patterns = provider.get_patterns()
        urls = [url for url in (acquisition_tasks or []) if url]

        # P26: resolve the policy stack once per provider (fail-open to inert).
        engine = getattr(self.pipeline, "gather_skip", None)
        engine_on = engine is not None and bool(getattr(engine, "enabled", False))
        enforce = bool(getattr(engine, "enforce", False))
        ledger = getattr(self.pipeline, "share_ledger", None)
        share_enabled = ledger is not None and bool(getattr(ledger, "enabled", False))

        digest = ""
        if urls and (engine_on or share_enabled):
            try:
                digest = patterns_hash(
                    key_pattern=patterns.key_pattern,
                    address_pattern=patterns.address_pattern,
                    endpoint_pattern=patterns.endpoint_pattern,
                    model_pattern=patterns.model_pattern,
                )
            except Exception as e:  # pragma: no cover - defensive
                logger.warning(f"[recovery] patterns digest failed for {name}: {e}")
                digest = ""

        skip_map: Dict[str, Any] = {}
        if engine_on and digest and urls:
            try:
                decisions = engine.decide(urls, provider=name, patterns_hash=digest)
                skip_map = {decision.url: decision for decision in decisions}
            except Exception as e:
                logger.warning(f"[recovery] gather-skip decision failed for {name}: {e}")
                skip_map = {}

        transport = "raw"
        if share_enabled:
            cfg = getattr(self.pipeline, "config", None)
            transport = str(getattr(getattr(cfg, "gather", None), "transport", "raw") or "raw")

        recovered_count = 0
        skip_suppressed = 0
        share_suppressed = 0

        for url in urls:
            try:
                decision = skip_map.get(url)
                if enforce and decision is not None and decision.skip:
                    skip_suppressed += 1
                    continue
                # P26: consult the share ledger after gather-skip (search-side
                # order, R7 D4).  A skip-suppressed provider registers no share
                # interest: another provider with a genuine coverage gap owns
                # the fetch, and the suppressed one already has coverage.
                if share_enabled and digest:
                    try:
                        if ledger.claim(url_hash(url), digest, transport, name):
                            share_suppressed += 1
                            continue
                    except Exception:
                        pass  # fail-open: create the task
                # Create acquisition task with patterns
                acquisition_task = TaskFactory.create_acquisition_task(name, url, patterns)
                stage_instance.put_task(acquisition_task)
                recovered_count += 1

            except Exception as e:
                logger.error(f"Failed to create acquisition task for {name}: {e}")

        # P26/D5: printed even when everything was suppressed — a fully
        # filtered recovery must be LOUDER, not silent (Н9).  The legacy base
        # substring "Recovered N acquisition tasks for <name>" stays stable.
        if urls:
            logger.info(
                f"Recovered {recovered_count} acquisition tasks for {name} "
                f"({source_label} urls {len(urls)}, skip-suppressed {skip_suppressed}, "
                f"share-suppressed {share_suppressed})"
            )

    def _get_stage_enum(self, stage_name: str) -> Optional[PipelineStage]:
        """Get stage enum from stage name

        Args:
            stage_name: Name of the stage

        Returns:
            PipelineStage enum or None if not found
        """
        try:
            return PipelineStage(stage_name)
        except ValueError:
            return None
