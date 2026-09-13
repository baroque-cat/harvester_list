#!/usr/bin/env python3

"""
Core Metrics - Monitoring and Performance Metrics

This module defines all metrics-related data models used throughout the application
for monitoring, performance tracking, and system health assessment.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict

from .enums import SystemState


# Base metrics classes
@dataclass
class BaseMetrics:
    """Base class for all metrics to reduce field duplication"""

    timestamp: float = field(default_factory=time.time)

    def age(self) -> float:
        """Get metrics age in seconds"""
        return time.time() - self.timestamp


@dataclass
class BaseStats(BaseMetrics):
    """Base class for statistics with common functionality"""

    @property
    def empty(self) -> bool:
        """Check if statistics are empty"""
        return self.total == 0

    @property
    def total(self) -> int:
        """Total count - to be overridden by subclasses"""
        return 0


# Core metrics models
@dataclass
class TaskMetrics(BaseStats):
    """Task execution metrics"""

    completed: int = 0
    failed: int = 0
    pending: int = 0
    running: int = 0

    @property
    def total(self) -> int:
        """Total tasks"""
        return self.completed + self.failed + self.pending + self.running

    @property
    def success_rate(self) -> float:
        """Task success rate"""
        processed = self.completed + self.failed
        return self.completed / processed if processed > 0 else 0.0

    @property
    def error_rate(self) -> float:
        """Task error rate"""
        processed = self.completed + self.failed
        return self.failed / processed if processed > 0 else 0.0

    def add_completed(self, count: int = 1) -> None:
        """Add completed tasks"""
        self.completed += count

    def add_failed(self, count: int = 1) -> None:
        """Add failed tasks"""
        self.failed += count


@dataclass
class StageMetrics(BaseMetrics):
    """Stage-level metrics"""

    name: str = ""
    running: bool = False
    disabled: bool = False

    # Task metrics
    tasks: TaskMetrics = field(default_factory=TaskMetrics)

    # Stage-specific fields
    queue_size: int = 0
    last_activity: float = 0.0
    workers: int = 0

    @property
    def total_processed(self) -> int:
        return self.tasks.completed

    @property
    def total_errors(self) -> int:
        return self.tasks.failed


@dataclass
class DateFillMetrics:
    """Per-run date-extraction fill counters, split by transport.

    ``date_fill_rate_api`` is the share of API search items that yielded a
    usable ``repo_pushed_at``; ``date_fill_rate_web`` is the share of gathered
    pages that yielded a ``file_commit_date``.  Both are drift monitors: an
    API rate well below 1.0 or a web rate collapsing towards 0 hint at a
    schema/layout change.
    """

    api_items: int = 0
    api_dated: int = 0
    web_pages: int = 0
    web_dated: int = 0

    # Guards the counters: stage workers record concurrently (verification
    # finding W1), while readers (StatusManager) may compute rates in parallel.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Fail-open parse counters are module-level; reset them so a fresh
        # metrics accumulator (per run) never inherits process-lifetime counts
        # (verification finding S1). Imported lazily to avoid a cycle.
        try:
            from search.client import reset_date_parse_stats

            reset_date_parse_stats()
        except ImportError:  # pragma: no cover - search package unavailable
            pass

    def record_api(self, dated: bool) -> None:
        """Count one API search item and whether it carried a usable date."""
        with self._lock:
            self.api_items += 1
            if dated:
                self.api_dated += 1

    def record_web(self, dated: bool) -> None:
        """Count one gathered page and whether it carried a file date."""
        with self._lock:
            self.web_pages += 1
            if dated:
                self.web_dated += 1

    @property
    def date_fill_rate_api(self) -> float:
        with self._lock:
            return self.api_dated / self.api_items if self.api_items > 0 else 0.0

    @property
    def date_fill_rate_web(self) -> float:
        with self._lock:
            return self.web_dated / self.web_pages if self.web_pages > 0 else 0.0

    def to_stats(self) -> Dict[str, Any]:
        """Flatten to a JSON-friendly mapping for run statistics."""
        return {
            "date_fill_rate_api": self.date_fill_rate_api,
            "date_fill_rate_web": self.date_fill_rate_web,
            "api_items": self.api_items,
            "api_dated": self.api_dated,
            "web_pages": self.web_pages,
            "web_dated": self.web_dated,
        }


@dataclass
class PipelineStatus:
    """Pipeline status information"""

    state: SystemState = SystemState.UNKNOWN
    active: int = 0
    total: int = 0

    # Core pipeline data
    stages: Dict[str, StageMetrics] = field(default_factory=dict)
    runtime: float = 0.0
    start: float = field(default_factory=time.monotonic)  # Use monotonic for interval calculations
    finished: bool = False

    # Date-extraction fill rates (empty when no observations were made).
    date_metrics: Dict[str, Any] = field(default_factory=dict)

    # Gather-skip decision counters/reasons (empty until wired).
    skip_metrics: Dict[str, Any] = field(default_factory=dict)

    # Repository metadata enrichment counters (empty while disabled).
    enrichment_metrics: Dict[str, Any] = field(default_factory=dict)

    # API pagination early-stop counters (empty while off).
    early_stop_metrics: Dict[str, Any] = field(default_factory=dict)

    # Key-ledger skip counters (empty while off).
    key_ledger_metrics: Dict[str, Any] = field(default_factory=dict)

    # Periodic re-check driver counters (empty while disabled).
    recheck_metrics: Dict[str, Any] = field(default_factory=dict)

    # Optional top-N priority candidates (empty while display_top_n == 0).
    prioritization_metrics: Dict[str, Any] = field(default_factory=dict)

    def queue_size(self) -> int:
        """Get total queue size across all stages"""
        return sum(stage.queue_size for stage in self.stages.values())

    def processed(self) -> int:
        """Get total processed tasks across all stages"""
        return sum(stage.total_processed for stage in self.stages.values())

    def errors(self) -> int:
        """Get total errors across all stages"""
        return sum(stage.total_errors for stage in self.stages.values())
