#!/usr/bin/env python3

"""
Base classes for pipeline stages.
Hybrid architecture with dependency injection and pure functional processing.
"""

import json
import math
import os
import queue
import sqlite3
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    Union,
    runtime_checkable,
)

from config.schemas import Config, StageConfig, TaskConfig
from constant.system import DEFAULT_SHUTDOWN_TIMEOUT
from core.enums import PipelineStage
from core.exceptions import RateLimitDeferral, TransientFetchError
from core.metrics import StageMetrics
from core.models import LinkMetadata, ProviderTask
from core.types import IAuthProvider, IProvider
from storage.task_queue import SqliteTaskQueue
from tools.logger import get_logger
from tools.ratelimit import RateLimiter
from tools.retry import ExponentialBackoff, RetryPolicy

logger = get_logger("stage")


@dataclass
class StageResources:
    """Resources injected into stages for dependency inversion"""

    limiter: RateLimiter
    providers: Dict[str, IProvider]
    config: Config
    task_configs: Dict[str, TaskConfig]
    auth: IAuthProvider
    registry: Any = None  # write-only link registry (never read for decisions)
    date_metrics: Any = None  # DateFillMetrics accumulator (fail-open observability)
    gather_skip: Any = None  # GatherSkipEngine (add-gather-skip; off by default)
    enrichment: Any = None  # RepoMetaEnricher (add-repo-meta-enrichment; off by default)
    early_stop: Any = None  # EarlyStopEngine (add-search-early-stop; off by default)
    key_ledger: Any = None  # KeyLedger (add-key-ledger; check-skip off by default)
    refine_governor: Any = None  # RefineGovernor (add-refine-fanout-governor; fail-open None == off)

    def is_enabled(self, provider: str, stage: str) -> bool:
        """Check if stage is enabled for provider"""
        config = self.task_configs.get(provider)
        if not config:
            return False
        return StageUtils.check(config, stage)


@dataclass
class StageOutput:
    """Pure functional output from stage processing"""

    task: ProviderTask
    new_tasks: List[Tuple[ProviderTask, str]] = field(default_factory=list)  # (task, target_stage)
    results: List[Tuple[str, str, Any]] = field(default_factory=list)  # (provider, type, data)
    links: List[Tuple[str, List[str]]] = field(default_factory=list)  # (provider, links)
    models: List[Tuple[str, str, List[str]]] = field(default_factory=list)  # (provider, key, models)
    link_metadata: List[Tuple[str, Dict[str, LinkMetadata]]] = field(
        default_factory=list
    )  # (provider, url -> metadata)

    def add_task(self, task: ProviderTask, target: str) -> None:
        """Add new task to be routed"""
        self.new_tasks.append((task, target))

    def add_result(self, provider: str, result_type: str, data: Any) -> None:
        """Add result to be saved"""
        self.results.append((provider, result_type, data))

    def add_links(self, provider: str, links: List[str]) -> None:
        """Add links to be saved"""
        self.links.append((provider, links))

    def add_models(self, provider: str, key: str, models: List[str]) -> None:
        """Add models to be saved"""
        self.models.append((provider, key, models))

    def add_link_metadata(self, provider: str, metadata: Dict[str, LinkMetadata]) -> None:
        """Attach per-link extracted freshness/size metadata for the registry."""
        if metadata:
            self.link_metadata.append((provider, metadata))


# Type alias for output handler function
OutputHandler = Callable[[StageOutput], None]


@runtime_checkable
class WorkerManageable(Protocol):
    """Protocol for stages that support dynamic worker management"""

    def adjust_workers(self, count: int) -> bool:
        """Adjust worker count for this stage

        Args:
            count: Target number of workers

        Returns:
            bool: True if adjustment was successful
        """
        ...

    def set_worker_count(self, count: int) -> bool:
        """Set worker count for this stage

        Args:
            count: Target number of workers

        Returns:
            bool: True if setting was successful
        """
        ...

    def get_worker_count(self) -> int:
        """Get current number of workers"""
        ...


class BasePipelineStage(ABC, WorkerManageable):
    """Base class for pipeline stages with hybrid architecture support"""

    def __init__(
        self,
        name: str,
        resources: StageResources,
        handler: OutputHandler,
        thread_count: int = 1,
        queue_size: int = 1000,
        max_retries: int = 0,
        dedup_max_size: int = 100_000,
        retry_policy: Optional[RetryPolicy] = None,
        queue_backend: str = "memory",
        queue_dir: Optional[str] = None,
        queue_visibility_timeout_s: float = 300.0,
        queue_max_age_hours: float = 24.0,
    ) -> None:
        self.name = name
        self.resources = resources
        self.handler = handler
        self.thread_count = thread_count

        # Task queue.  Default backend stays the historical in-memory FIFO so
        # behavior is byte-identical unless ``queue.backend: sqlite`` is set
        # (fix-queue-persistence-under-load, design D8).
        self.queue_degraded = False
        self._durable_queue = False
        self.queue = self._build_queue(
            queue_backend,
            queue_dir,
            queue_size,
            queue_visibility_timeout_s,
            queue_max_age_hours,
        )

        # Task deduplication (bounded)
        self.processed: set = set()
        self.processed_order = deque()
        self.dedup_max_size = max(1000, int(dedup_max_size))
        self.dedup_lock = threading.Lock()

        # Worker threads
        self.workers: List[threading.Thread] = []
        self.running = False
        self.accepting = True

        # Maximum number of retries
        self.max_retries = max(max_retries, 0)

        # Retry policy.  The dedup gate in ``put_task`` enforces the real bound
        # (it rejects requeues once ``attempts > max_retries``); the policy is
        # given one extra step so the boundary attempt reaches the gate and is
        # dropped loudly (counted) instead of being abandoned in the loop.
        self.retry_policy = retry_policy or ExponentialBackoff(max_retries=self.max_retries + 1)

        # Statistics
        self.total_processed = 0
        self.total_errors = 0
        self.last_activity = time.time()
        self.start_time = time.time()

        # Failure-handling observability (failure-handling spec).  In legacy
        # mode the detection counter stays inert; requeue/drop counters reflect
        # the worker loop only when a typed failure actually propagates.
        self.failure_empties_detected = 0
        self.tasks_requeued = 0
        self.tasks_dropped_max_retries = 0
        # Tasks deferred on a published rate limit (design D4): DEFER is a third
        # outcome distinct from requeue and drop.
        self.tasks_deferred = 0
        # Bounded stage-wide pause gate for actor-scoped secondary/abuse limits.
        self._defer_pause_until = 0.0
        # Durable-backend write failures (a dropped task is always counted).
        self.tasks_dropped_backend_errors = 0

        # Work state tracking
        self.active_workers = 0
        self.work_lock = threading.Lock()

        # Thread safety
        self.stats_lock = threading.Lock()

        # Thread lifecycle tracking
        self.zombie_threads = []

        logger.info(f"Created stage: {name}, threads: {thread_count}, queue: {queue_size}")

    def _build_queue(
        self,
        queue_backend: str,
        queue_dir: Optional[str],
        queue_size: int,
        visibility_timeout_s: float,
        max_age_hours: float,
    ) -> Any:
        """Build the stage queue for the selected backend (design D7/D11).

        The memory branch is untouched.  The sqlite branch is fail-open: any
        open/bootstrap failure logs an ERROR naming the stage, marks the stage
        degraded and falls back to a functional in-memory queue so a disk
        problem never zeroes the run.
        """
        if str(queue_backend).strip().lower() != "sqlite":
            return queue.Queue(maxsize=queue_size)

        try:
            if not queue_dir:
                raise ValueError("queue_dir is required for the sqlite backend")

            # Lazily imported: only the durable path needs the task serializers.
            from stage.factory import TaskFactory

            path = os.path.join(str(queue_dir), f"{self.name}_queue.sqlite")
            durable = SqliteTaskQueue(
                path,
                serializer=lambda task: json.dumps(task.to_dict(), ensure_ascii=False),
                deserializer=TaskFactory.from_dict,
                name=self.name,
                visibility_timeout_s=visibility_timeout_s,
                max_age_hours=max_age_hours,
            )
            self._durable_queue = True
            return durable
        except (OSError, sqlite3.Error, ValueError) as e:
            logger.error(
                f"[{self.name}] failed to open durable task queue, falling back to memory backend: {e}"
            )
            self.queue_degraded = True
            return queue.Queue(maxsize=queue_size)

    def start(self) -> None:
        """Start worker threads"""
        if self.running:
            return

        self.running = True
        self.accepting = True

        for i in range(self.thread_count):
            worker = threading.Thread(target=self._worker_loop, name=f"{self.name}-worker-{i+1}", daemon=True)
            worker.start()
            self.workers.append(worker)

        logger.info(f"[{self.name}] started {len(self.workers)} workers")

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        """Stop worker threads with enhanced tracking"""
        if not self.running:
            return

        # Stop accepting new tasks
        self.accepting = False
        logger.info(f"[{self.name}] stopping, waiting for {len(self.workers)} workers")

        # Wait for queue to drain
        queue_timeout = timeout * 0.3
        start_time = time.time()
        while not self.queue.empty() and time.time() - start_time < queue_timeout:
            time.sleep(0.1)

        # Stop workers with tracking
        self.running = False
        worker_timeout = timeout * 0.6
        alive_workers = []

        for worker in self.workers:
            if worker.is_alive():
                worker.join(timeout=worker_timeout / max(len(self.workers), 1))
                if worker.is_alive():
                    worker_name = worker.name if hasattr(worker, "name") else f"worker-{id(worker)}"
                    alive_workers.append(worker_name)

        # Track zombie threads for monitoring
        if alive_workers:
            self.zombie_threads = alive_workers
            logger.warning(f"[{self.name}] {len(alive_workers)} workers did not stop gracefully")
        else:
            self.zombie_threads = []
            logger.info(f"[{self.name}] all workers stopped gracefully")

    def put_task(self, task: ProviderTask) -> bool:
        """Add task to queue with deduplication check"""
        if not self.accepting:
            logger.warning(f"[{self.name}] not accepting tasks, discard: {task}")
            return False

        # Generate task ID for deduplication
        task_id = self._generate_id(task)

        # Check if task already processed
        with self.dedup_lock:
            # Logic: attempts == 0 means new task, but same task already queued
            if task_id in self.processed and (task.attempts == 0 or task.attempts > self.max_retries):
                if task.attempts > self.max_retries:
                    with self.stats_lock:
                        self.tasks_dropped_max_retries += 1
                    logger.warning(
                        f"[{self.name}] task=[{task_id}] discarded, max retries=[{self.max_retries}] reached"
                    )
                return False

        # Try to add to queue
        try:
            if self._durable_queue:
                # Commit-per-put: returning normally means the task is durable
                # (design D6).  There is no capacity bound, so queue.Full is
                # unreachable on this branch.
                self.queue.put(
                    task,
                    timeout=1.0,
                    dedup_id=task_id,
                    created_at=getattr(task, "created_at", None),
                    attempts=getattr(task, "attempts", 0),
                )
            else:
                self.queue.put(task, timeout=1.0)
        except queue.Full:
            logger.warning(f"[{self.name}] queue is full")
            return False
        except Exception as e:
            if self._durable_queue:
                # Runtime storage failure: loud + counted, never silent (S6).
                with self.stats_lock:
                    self.tasks_dropped_backend_errors += 1
                logger.warning(f"[{self.name}] dropped task, durable queue write failed: {e}")
                return False
            raise

        with self.dedup_lock:
            if task_id not in self.processed:
                self.processed.add(task_id)
                self.processed_order.append(task_id)
                # Evict oldest when exceeding cap to avoid unbounded growth
                if len(self.processed) > self.dedup_max_size and self.processed_order:
                    oldest = self.processed_order.popleft()
                    if oldest != task_id:
                        self.processed.discard(oldest)

        return True

    def defer_task(self, task: ProviderTask, wait_s: Optional[float] = None) -> bool:
        """Return a rate-limit-deferred task to pending WITHOUT consuming budget.

        The third outcome seam (design D4).  A deferral is not a duplicate, so
        the dedup gate is bypassed entirely; ``attempts`` is preserved unchanged,
        as are the original ``created_at`` and ``dedup_id`` so the durable queue's
        ``max_age_hours`` age gate stays anchored to first creation (the natural
        ceiling that stops a deferred task circulating forever).

        ``wait_s`` is the BOUNDED wait already clamped by ``_effective_defer_wait``
        (design D5).  It is optional so plain re-queue callers stay valid, but when
        supplied it is named in the WARNING: the effective deferral duration is the
        one number an operator needs during a soak, and it is otherwise invisible.
        """
        task_id = self._generate_id(task)
        try:
            if self._durable_queue:
                self.queue.put(
                    task,
                    timeout=1.0,
                    dedup_id=task_id,
                    created_at=getattr(task, "created_at", None),
                    attempts=getattr(task, "attempts", 0),
                )
            else:
                self.queue.put(task, timeout=1.0)
        except queue.Full:
            with self.stats_lock:
                self.tasks_dropped_backend_errors += 1
            logger.warning(f"[{self.name}] queue is full, deferred task dropped: {task}")
            return False
        except Exception as e:
            if self._durable_queue:
                with self.stats_lock:
                    self.tasks_dropped_backend_errors += 1
                logger.warning(f"[{self.name}] dropped deferred task, durable queue write failed: {e}")
                return False
            raise

        with self.stats_lock:
            self.tasks_deferred += 1
        cap = self._defer_wait_cap()
        if wait_s is None:
            wait_detail = "bounded wait: n/a"
        elif math.isinf(cap):
            wait_detail = f"bounded wait: {float(wait_s):.1f}s (no configured cap)"
        else:
            wait_detail = f"bounded wait: {float(wait_s):.1f}s (cap {cap:.1f}s)"
        logger.warning(
            f"[{self.name}] deferred task on rate limit, provider: {getattr(task, 'provider', '')}, "
            f"{wait_detail}, task: {task}"
        )
        return True

    def _defer_wait_cap(self) -> float:
        """Configured refusal-wait ceiling in seconds (design D5).

        Returns ``inf`` when the gather config is unreachable so the caller
        degrades to the published wait instead of inventing a tighter bound.
        """
        try:
            return float(self.resources.config.gather.max_refusal_wait_s)
        except Exception:
            return float("inf")

    def _effective_defer_wait(self, wait_s: float) -> float:
        """Clamp a deferral wait to the configured refusal cap (design D5)."""
        return max(0.0, min(float(wait_s), self._defer_wait_cap()))

    def is_finished(self) -> bool:
        """Check if stage is finished processing"""
        # Stage is finished if:
        # 1. Queue is empty
        # 2. No workers are actively processing tasks
        return self.queue.empty() and not self._has_active_workers()

    def get_stats(self) -> "StageMetrics":
        """Get stage statistics"""

        with self.stats_lock:
            metrics = StageMetrics(
                name=self.name,
                running=self.running,
                disabled=False,  # Active stages are not disabled
                queue_size=self.queue.qsize(),
                last_activity=self.last_activity,
                workers=len(self.workers),
            )
            # Set task metrics
            metrics.tasks.completed = self.total_processed
            metrics.tasks.failed = self.total_errors

            # Failure-handling observability counters (failure-handling spec).
            metrics.failure_empties_detected = self.failure_empties_detected
            metrics.tasks_requeued = self.tasks_requeued
            metrics.tasks_dropped_max_retries = self.tasks_dropped_max_retries
            metrics.tasks_dropped_backend_errors = self.tasks_dropped_backend_errors
            metrics.tasks_deferred = self.tasks_deferred

            return metrics

    def has_zombie_threads(self) -> bool:
        """Check if there are threads that failed to stop"""
        return len(self.zombie_threads) > 0

    def get_zombie_count(self) -> int:
        """Get count of zombie threads"""
        return len(self.zombie_threads)

    def get_pending_tasks(self) -> List[ProviderTask]:
        """Get all pending tasks (for persistence).

        The durable backend answers with a non-destructive query (S14); the
        memory backend keeps the historical drain-and-put-back path verbatim.
        """
        snapshot = getattr(self.queue, "snapshot_pending", None)
        if callable(snapshot):
            return snapshot()

        tasks = []
        temp_tasks = []

        # Extract all tasks without blocking
        while not self.queue.empty():
            try:
                task = self.queue.get_nowait()
                tasks.append(task)
                temp_tasks.append(task)
            except queue.Empty:
                break

        # Put tasks back
        for task in temp_tasks:
            try:
                self.queue.put_nowait(task)
            except queue.Full:
                logger.warning(f"[{self.name}] lost task during persistence: {task.task_id}")

        return tasks

    def is_busy(self) -> bool:
        """Check if stage is currently processing tasks"""
        return not self.queue.empty() or self._has_active_workers()

    def stop_accepting(self) -> None:
        """Stop accepting new tasks"""
        self.accepting = False

    def get_worker_count(self) -> int:
        """Get current number of workers"""
        return len(self.workers)

    def adjust_workers(self, count: int) -> bool:
        """Adjust worker count for this stage

        Args:
            count: Target number of workers

        Returns:
            bool: True if adjustment was successful
        """
        if count < 0:
            logger.warning(f"[{self.name}] invalid worker count: {count}")
            return False

        current_count = len(self.workers)
        if count == current_count:
            return True

        if count > current_count:
            return self._add_workers(count - current_count)
        else:
            return self._remove_workers(current_count - count)

    def set_worker_count(self, count: int) -> bool:
        """Set worker count for this stage

        Args:
            count: Target number of workers

        Returns:
            bool: True if setting was successful
        """
        return self.adjust_workers(count)

    def _failure_handling_mode(self) -> str:
        """Return the configured ``failure_handling`` mode (design D3)."""
        config = getattr(self.resources, "config", None)
        pipeline = getattr(config, "pipeline", None)
        mode = getattr(pipeline, "failure_handling", "shadow")
        return mode if mode in ("legacy", "shadow", "strict") else "shadow"

    def _detect_failure_empty(self, task: ProviderTask, error: Exception) -> None:
        """Count and log a detected failure-empty (shadow and strict only)."""
        if self._failure_handling_mode() == "legacy":
            return
        with self.stats_lock:
            self.failure_empties_detected += 1
        logger.warning(
            f"[{self.name}] failure-empty detected, provider: {getattr(task, 'provider', '')}, "
            f"task: {task}, error: {error}"
        )

    def process_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Template method for task processing with common workflow."""
        # Step 1: Validate task type
        if not self._validate_task_type(task):
            logger.error(f"[{self.name}] invalid task type: {type(task)}")
            return None

        # Step 2: Pre-processing hook
        if not self._pre_process(task):
            return None

        # Step 3: Execute core processing (implemented by subclasses)
        try:
            result = self._execute_task(task)

            # Step 4: Post-processing hook
            if result:
                result = self._post_process(task, result)

            return result

        except RateLimitDeferral:
            # A deferral is neither an answer nor a task fault: it must never be
            # swallowed by the failure-empty handler or mode-collapsed in ANY
            # failure_handling mode (failure-handling S18/S21).
            raise

        except TransientFetchError as e:
            # Typed transient failure: shadow counts/logs then swallows (same
            # outcome as legacy); strict lets it escape so the worker loop's
            # existing retry branch fires (design D3).
            self._detect_failure_empty(task, e)
            if self._failure_handling_mode() == "strict":
                raise
            return None

        except Exception as e:
            logger.error(f"[{self.name}] task processing failed: {e}")
            return self._handle_processing_error(task, e)

    @abstractmethod
    def _validate_task_type(self, task: ProviderTask) -> bool:
        """Validate that the task is of the correct type for this stage."""
        pass

    @abstractmethod
    def _execute_task(self, task: ProviderTask) -> Optional[StageOutput]:
        """Execute the core task processing logic."""
        pass

    def _pre_process(self, task: ProviderTask) -> bool:
        """Pre-processing hook. Return False to skip processing."""
        return True

    def _post_process(self, task: ProviderTask, result: StageOutput) -> StageOutput:
        """Post-processing hook. Can modify the result."""
        return result

    def _handle_processing_error(self, task: ProviderTask, error: Exception) -> Optional[StageOutput]:
        """Handle processing errors. Return None by default."""
        return None

    @abstractmethod
    def _generate_id(self, task: ProviderTask) -> str:
        """Generate unique task identifier for deduplication"""
        pass

    def _worker_loop(self) -> None:
        """Main worker thread loop with pure functional processing"""
        while self.running:
            # Actor-scoped secondary/abuse refusals pause the whole stage for a
            # bounded interval so sibling workers stop claiming new rows (S16).
            with self.stats_lock:
                pause_remaining = self._defer_pause_until - time.time()
            if pause_remaining > 0:
                time.sleep(min(pause_remaining, 1.0))
                continue

            try:
                # Get task with timeout
                task = self.queue.get(timeout=1.0)

                # Mark worker as active
                with self.work_lock:
                    self.active_workers += 1

                # Update activity time
                with self.stats_lock:
                    self.last_activity = time.time()

                # Process task
                try:
                    output = self.process_task(task)

                    # Handle output if returned
                    if output:
                        self.handler(output)

                    # Update success statistics
                    with self.stats_lock:
                        self.total_processed += 1

                except RateLimitDeferral as d:
                    # DEFER: sleep a bounded interval INSIDE the claim (bounded by
                    # the D5 invariant WAIT_CAP < visibility_timeout_s), then
                    # return the row to pending without touching attempts.  No
                    # total_errors / tasks_requeued / total_processed: a deferral
                    # is not a completion (failure-handling S19).
                    wait = self._effective_defer_wait(d.wait_s)
                    if d.stage_pause:
                        with self.stats_lock:
                            self._defer_pause_until = max(self._defer_pause_until, time.time() + wait)
                    if wait > 0:
                        time.sleep(wait)
                    self.defer_task(task, wait_s=wait)

                except Exception as e:
                    logger.error(f"[{self.name}] error processing task: {e}")

                    # Check if task should be retried using policy
                    if self.retry_policy.should_retry(task.attempts, e):
                        # Get delay from policy
                        delay = self.retry_policy.get_delay(task.attempts)
                        if delay > 0:
                            time.sleep(delay)

                        task.attempts += 1
                        success = self.put_task(task)
                        if success:
                            with self.stats_lock:
                                self.tasks_requeued += 1
                        status = "successfully" if success else "failed"
                        logger.warning(f"[{self.name}] requeued {status} after {delay:.1f}s delay, task: {task}")

                    # Update error statistics
                    with self.stats_lock:
                        self.total_errors += 1
                        self.total_processed += 1

                finally:
                    # Mark worker as inactive
                    with self.work_lock:
                        self.active_workers -= 1

                    # Mark task as done
                    self.queue.task_done()

            except queue.Empty:
                # Timeout waiting for task, continue loop
                continue
            except Exception as e:
                logger.error(f"[{self.name}] worker error: {e}")

    def _has_active_workers(self) -> bool:
        """Check if any workers are currently active"""
        with self.work_lock:
            return self.active_workers > 0

    def _add_workers(self, count: int) -> bool:
        """Add new worker threads

        Args:
            count: Number of workers to add

        Returns:
            bool: True if workers were added successfully
        """
        if not self.running:
            logger.warning(f"[{self.name}] cannot add workers: stage not running")
            return False

        try:
            current_worker_count = len(self.workers)
            for i in range(count):
                worker_id = current_worker_count + i + 1
                worker = threading.Thread(target=self._worker_loop, name=f"{self.name}-worker-{worker_id}", daemon=True)
                worker.start()
                self.workers.append(worker)

            logger.info(f"[{self.name}] added {count} workers (total: {len(self.workers)})")
            return True

        except Exception as e:
            logger.error(f"[{self.name}] failed to add workers: {e}")
            return False

    def _remove_workers(self, count: int) -> bool:
        """Remove worker threads gracefully

        Args:
            count: Number of workers to remove

        Returns:
            bool: True if workers were removed successfully
        """
        if count <= 0:
            return True

        # Don't remove more workers than we have
        count = min(count, len(self.workers))
        if count == 0:
            return True

        try:
            # Mark workers for removal by reducing the worker list
            # The actual threads will finish their current tasks and exit naturally
            workers_to_remove = self.workers[-count:]
            self.workers = self.workers[:-count]

            # Wait for removed workers to finish with timeout
            timeout_per_worker = 2.0
            for worker in workers_to_remove:
                if worker.is_alive():
                    worker.join(timeout=timeout_per_worker)

            logger.info(f"[{self.name}] removed {count} workers (total: {len(self.workers)})")
            return True

        except Exception as e:
            logger.error(f"[{self.name}] failed to remove workers: {e}")
            return False


class StageUtils:
    """Stage configuration utility class"""

    _names_cache: Optional[List[str]] = None

    @classmethod
    def get_names(cls) -> List[str]:
        """Get all possible stage names"""
        if cls._names_cache is None:
            names = []

            # Use reflection to get all boolean attributes from StageConfig
            for name in dir(StageConfig):
                if not name.startswith("_"):
                    try:
                        attr = getattr(StageConfig, name)
                        # Check if it's a boolean attribute or annotated as bool
                        if isinstance(attr, bool) or (
                            hasattr(StageConfig, "__annotations__")
                            and name in StageConfig.__annotations__
                            and StageConfig.__annotations__[name] == bool
                        ):
                            names.append(name)
                    except (AttributeError, TypeError):
                        continue

            cls._names_cache = names

        return cls._names_cache.copy()

    @classmethod
    def get_enabled(cls, config: TaskConfig) -> List[str]:
        """Get enabled stage names from task config (based on list())"""
        return [stage.value for stage in cls._list(config)]

    @classmethod
    def clear_cache(cls) -> None:
        """Clear cached stage names (for testing)"""
        cls._names_cache = None

    @classmethod
    def check(cls, config: TaskConfig, stage: Union[PipelineStage, str]) -> bool:
        """Check if stage is enabled in configuration

        Args:
            config: Task configuration to check
            stage: Stage to check (PipelineStage enum or string name)

        Returns:
            bool: True if stage is enabled, False otherwise
        """
        if not config or not config.stages:
            return False

        # Convert stage to attribute name
        attr_name = cls._get_attr_name(stage)
        if not attr_name:
            return False

        # Use dynamic attribute access to avoid hardcoding
        return getattr(config.stages, attr_name, False)

    @classmethod
    def _list(cls, config: TaskConfig) -> List[PipelineStage]:
        """Get list of enabled stages as PipelineStage enums

        Args:
            config: Task configuration to check

        Returns:
            List[PipelineStage]: List of enabled stages
        """
        if not config or not config.stages:
            return []

        enabled_stages = []
        for stage_enum in PipelineStage:
            if getattr(config.stages, stage_enum.value, False):
                enabled_stages.append(stage_enum)

        return enabled_stages

    @classmethod
    def all(cls, config: TaskConfig, stages: List[Union[PipelineStage, str]]) -> bool:
        """Check if all specified stages are enabled

        Args:
            config: Task configuration to check
            stages: List of stages to check

        Returns:
            bool: True if all stages are enabled, False otherwise
        """
        if not stages:
            return True

        return all(cls.check(config, stage) for stage in stages)

    @classmethod
    def any(cls, config: TaskConfig, stages: List[Union[PipelineStage, str]]) -> bool:
        """Check if any of the specified stages are enabled

        Args:
            config: Task configuration to check
            stages: List of stages to check

        Returns:
            bool: True if any stage is enabled, False otherwise
        """
        if not stages:
            return False

        return any(cls.check(config, stage) for stage in stages)

    @classmethod
    def _get_attr_name(cls, stage: Union[PipelineStage, str]) -> str:
        """Convert stage to StageConfig attribute name

        Args:
            stage: Stage as enum or string

        Returns:
            str: Attribute name or empty string if invalid
        """
        if isinstance(stage, PipelineStage):
            return stage.value
        elif isinstance(stage, str):
            # Validate that the string is a valid stage name
            try:
                PipelineStage(stage)
                return stage
            except ValueError:
                return ""
        else:
            return ""
