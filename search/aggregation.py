#!/usr/bin/env python3

"""Shared search-response layer: TTL/LRU cache + singleflight (add-search-aggregation).

Lives strictly below the stage-facing dispatchers (``search.client``), so the
credential-rotation loop, failure taxonomy, baskets, shards and registry hooks
are all untouched (design D1).  A response is a pure function of
``(transport, wire_query, page)``; identical fetches within a transport TTL are
served from the store and concurrent identical fetches coalesce onto one HTTP
request.

Invariants:
- I2: a leader failure propagates verbatim to every joiner and stores nothing.
- I3: a cache hit never consumes a rate-limit bucket, reports adaptively or
  touches credential cooldown state (hits bypass the transport function).
- I4: only fully successful returns are storable; any raised exception bypasses
  the store structurally (the taxonomy raises before this layer sees a value).
- I6: in-memory only - no state survives a restart.
"""

import json
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from search.querykey import fingerprint, wire_query
from storage.atomic import AtomicFileWriter
from tools.logger import get_logger

logger = get_logger("search")

DECISION_LOG_FILENAME = "aggregation_decisions.jsonl"

DEFAULT_TTL_WEB_S = 120.0
DEFAULT_TTL_API_S = 300.0
DEFAULT_MAX_BYTES = 64 * 1024 * 1024
DEFAULT_JOIN_TIMEOUT_S = 60.0

_VALID_MODES = ("off", "shadow", "on")


@dataclass
class AggregationMetrics:
    """Mutable counters exposed through ``PipelineStatus.aggregation_metrics``."""

    hits: int = 0
    misses: int = 0
    joins: int = 0
    join_timeouts: int = 0
    evictions: int = 0
    entries: int = 0
    bytes: int = 0
    poisoned_rejected: int = 0
    shadow_comparisons: int = 0
    shadow_warnings: int = 0
    shadow_jaccard: List[float] = field(default_factory=list)

    @property
    def shadow_jaccard_min(self) -> float:
        return min(self.shadow_jaccard) if self.shadow_jaccard else 1.0

    @property
    def shadow_jaccard_p95(self) -> float:
        if not self.shadow_jaccard:
            return 1.0
        ordered = sorted(self.shadow_jaccard)
        index = min(len(ordered) - 1, max(0, math.ceil(0.95 * len(ordered)) - 1))
        return ordered[index]

    def record_shadow_jaccard(self, value: float) -> None:
        self.shadow_jaccard.append(float(value))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "joins": self.joins,
            "join_timeouts": self.join_timeouts,
            "evictions": self.evictions,
            "entries": self.entries,
            "bytes": self.bytes,
            "poisoned_rejected": self.poisoned_rejected,
            "shadow_comparisons": self.shadow_comparisons,
            "shadow_warnings": self.shadow_warnings,
            "shadow_jaccard_min": self.shadow_jaccard_min,
            "shadow_jaccard_p95": self.shadow_jaccard_p95,
            "aggregatable_pairs": get_aggregatable_pairs(),
        }


@dataclass
class _Entry:
    """One stored successful response plus its freshness/size bookkeeping."""

    stored_at: float
    ttl: float
    results: List[str]
    total: int
    content: str
    metadata: Optional[Dict[str, Any]]
    size: int


class _Flight:
    """In-flight leader tracking for singleflight coalescing."""

    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Optional[Tuple[List[str], int, str, Optional[Dict[str, Any]]]] = None
        self.error: Optional[BaseException] = None


class SearchAggregator:
    """TTL + byte-capped LRU store with singleflight coalescing."""

    def __init__(
        self,
        mode: str = "off",
        ttl_web_s: float = DEFAULT_TTL_WEB_S,
        ttl_api_s: float = DEFAULT_TTL_API_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        clock: Optional[Callable[[], float]] = None,
        workspace: Optional[str] = None,
    ) -> None:
        resolved = str(mode).strip().lower()
        if resolved not in _VALID_MODES:
            raise ValueError("aggregation.mode must be one of: off, shadow, on")
        self.mode = resolved
        self.ttl_web_s = float(ttl_web_s)
        self.ttl_api_s = float(ttl_api_s)
        self.max_bytes = int(max_bytes)
        self.join_timeout_s = float(join_timeout_s)
        self.clock: Callable[[], float] = clock or time.monotonic
        self.workspace = workspace

        self.metrics = AggregationMetrics()
        self._lock = threading.Lock()
        self._cache: "OrderedDict[Tuple[Any, ...], _Entry]" = OrderedDict()
        self._flights: Dict[Tuple[Any, ...], _Flight] = {}
        self._bytes = 0

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def call(
        self,
        use_api: bool,
        query: str,
        page: int,
        real_fn: Callable[[], Tuple[List[str], int, str]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[str], int, str]:
        """Serve ``(results, total, content)`` for a (transport, query, page)."""
        if self.mode == "off":
            return real_fn()

        use_api = bool(use_api)
        key = (use_api, wire_query(query, use_api), int(page))

        if self.mode == "shadow":
            return self._call_shadow(key, use_api, query, page, real_fn, metadata)
        return self._call_on(key, use_api, query, page, real_fn, metadata)

    # ------------------------------------------------------------------
    # On mode: cache + singleflight
    # ------------------------------------------------------------------
    def _call_on(self, key, use_api, query, page, real_fn, metadata):
        now = self.clock()
        entry = None
        role = "leader"
        flight = None

        with self._lock:
            entry = self._lookup(key, now)
            if entry is not None:
                self.metrics.hits += 1
            else:
                flight = self._flights.get(key)
                if flight is None:
                    flight = _Flight()
                    self._flights[key] = flight
                    role = "leader"
                else:
                    role = "joiner"

        if entry is not None:
            return self._materialize(entry.results, entry.total, entry.content, entry.metadata, metadata)
        if role == "joiner":
            return self._join(key, use_api, flight, real_fn, metadata)
        return self._lead(key, use_api, flight, real_fn, metadata)

    def _lead(self, key, use_api, flight, real_fn, metadata):
        with self._lock:
            self.metrics.misses += 1

        try:
            result = real_fn()
        except BaseException as exc:  # verbatim propagation to every joiner (I2)
            with self._lock:
                self._flights.pop(key, None)
                flight.error = exc
                flight.event.set()
            raise

        normalized = self._normalize(result, metadata)
        with self._lock:
            self._flights.pop(key, None)
            try:
                if normalized is not None:
                    results, total, content, meta_snapshot = normalized
                    self._store_entry(key, use_api, results, total, content, meta_snapshot)
                    flight.result = (results, total, content, meta_snapshot)
            except BaseException as exc:
                # A store fault must still wake joiners with the failure rather
                # than let them stall until join_timeout_s (I2).
                flight.error = exc
                raise
            finally:
                flight.event.set()

        if normalized is None:
            return self._raw_serve(result)
        results, total, content, meta_snapshot = normalized
        return self._materialize(results, total, content, meta_snapshot, metadata)

    def _join(self, key, use_api, flight, real_fn, metadata):
        if flight.event.wait(timeout=self.join_timeout_s):
            with self._lock:
                self.metrics.joins += 1
                error = flight.error
                payload = flight.result
            if error is not None:
                raise error
            if payload is None:
                return self._direct(key, use_api, real_fn, metadata)
            results, total, content, meta_snapshot = payload
            return self._materialize(results, total, content, meta_snapshot, metadata)

        # Join timeout: never drop a task - issue our own real request instead
        # (worst case equals pre-change behavior).  No flight bookkeeping so the
        # original leader stays authoritative.
        with self._lock:
            self.metrics.join_timeouts += 1
        return self._direct(key, use_api, real_fn, metadata)

    def _direct(self, key, use_api, real_fn, metadata):
        with self._lock:
            self.metrics.misses += 1
        result = real_fn()
        normalized = self._normalize(result, metadata)
        if normalized is None:
            return self._raw_serve(result)
        results, total, content, meta_snapshot = normalized
        with self._lock:
            self._store_entry(key, use_api, results, total, content, meta_snapshot)
        return self._materialize(results, total, content, meta_snapshot, metadata)

    # ------------------------------------------------------------------
    # Shadow mode: always live, compare against a would-be hit (D6)
    # ------------------------------------------------------------------
    def _call_shadow(self, key, use_api, query, page, real_fn, metadata):
        now = self.clock()
        with self._lock:
            entry = self._lookup(key, now)

        live = real_fn()

        if entry is not None:
            self._compare_and_log(use_api, query, page, entry, live)

        normalized = self._normalize(live, metadata)
        if normalized is None:
            return self._raw_serve(live)
        results, total, content, meta_snapshot = normalized
        with self._lock:
            self._store_entry(key, use_api, results, total, content, meta_snapshot)
        return self._materialize(results, total, content, meta_snapshot, metadata)

    def _compare_and_log(self, use_api, query, page, entry, live):
        try:
            live_results: List[str] = []
            live_total = 0
            if isinstance(live, (tuple, list)) and len(live) >= 1 and isinstance(live[0], (list, tuple)):
                live_results = list(live[0])
            if isinstance(live, (tuple, list)) and len(live) >= 2:
                live_total = int(live[1])

            cached_set = set(entry.results)
            live_set = set(live_results)
            union = cached_set | live_set
            jaccard = (len(cached_set & live_set) / len(union)) if union else 1.0
            total_delta = live_total - int(entry.total)

            record = {
                "ts": time.time(),
                "transport": "api" if use_api else "web",
                "fingerprint": fingerprint(query, use_api),
                "page": int(page),
                "jaccard": jaccard,
                "total_delta": total_delta,
                "n_live": len(live_set),
                "n_cached": len(cached_set),
                "mode": "shadow",
            }
            self._append_decision(record)
            with self._lock:
                self.metrics.shadow_comparisons += 1
                self.metrics.record_shadow_jaccard(jaccard)
        except Exception as exc:  # comparison/logging is fail-open
            with self._lock:
                self.metrics.shadow_warnings += 1
            logger.warning(f"[aggregation] shadow comparison failed (fail-open): {exc}")

    def _append_decision(self, record: Dict[str, Any]) -> None:
        if not self.workspace:
            return
        path = os.path.join(self.workspace, DECISION_LOG_FILENAME)
        AtomicFileWriter.append_atomic(path, [json.dumps(record, ensure_ascii=True, sort_keys=True)])

    # ------------------------------------------------------------------
    # Store internals (caller holds ``self._lock`` unless noted)
    # ------------------------------------------------------------------
    def _ttl_for(self, use_api: bool) -> float:
        return self.ttl_api_s if use_api else self.ttl_web_s

    def _lookup(self, key, now: float) -> Optional[_Entry]:
        entry = self._cache.get(key)
        if entry is None:
            return None
        if now - entry.stored_at > entry.ttl:
            self._cache.pop(key, None)
            self._bytes -= entry.size
            self._sync_size_metrics()
            return None
        self._cache.move_to_end(key)
        return entry

    def _store_entry(self, key, use_api, results, total, content, meta_snapshot) -> None:
        entry = _Entry(
            stored_at=self.clock(),
            ttl=self._ttl_for(use_api),
            results=list(results),
            total=total,
            content=content,
            metadata=dict(meta_snapshot) if meta_snapshot else None,
            size=self._entry_size(results, content, meta_snapshot),
        )
        old = self._cache.pop(key, None)
        if old is not None:
            self._bytes -= old.size
        self._cache[key] = entry
        self._bytes += entry.size
        while self._bytes > self.max_bytes and self._cache:
            _, evicted = self._cache.popitem(last=False)
            self._bytes -= evicted.size
            self.metrics.evictions += 1
        self._sync_size_metrics()

    def _sync_size_metrics(self) -> None:
        self.metrics.entries = len(self._cache)
        self.metrics.bytes = self._bytes

    @staticmethod
    def _entry_size(results, content, meta_snapshot) -> int:
        size = 64  # fixed per-entry overhead
        if isinstance(content, str):
            size += len(content)
        size += sum(len(url) for url in results)
        if meta_snapshot:
            size += len(meta_snapshot) * 48
        return size

    # ------------------------------------------------------------------
    # Payload hygiene / copies
    # ------------------------------------------------------------------
    def _normalize(self, result, metadata):
        """Validate a successful return; reject defensively (poisoned_rejected)."""
        if not isinstance(result, (tuple, list)) or len(result) != 3:
            with self._lock:
                self.metrics.poisoned_rejected += 1
            return None
        results, total, content = result
        if not isinstance(results, (list, tuple)):
            with self._lock:
                self.metrics.poisoned_rejected += 1
            return None
        try:
            total = int(total)
        except (TypeError, ValueError):
            with self._lock:
                self.metrics.poisoned_rejected += 1
            return None
        meta_snapshot = dict(metadata) if metadata else None
        return list(results), total, content if isinstance(content, str) else "", meta_snapshot

    @staticmethod
    def _materialize(results, total, content, meta_snapshot, metadata):
        """Hand a consumer its own mutable copy; content stays immutable/shared."""
        out = list(results)
        if metadata is not None and meta_snapshot is not None:
            metadata.update(dict(meta_snapshot))
        return out, int(total), content

    @staticmethod
    def _raw_serve(result):
        if isinstance(result, (tuple, list)) and len(result) == 3:
            first = list(result[0]) if isinstance(result[0], (list, tuple)) else result[0]
            return first, result[1], result[2]
        return result


# ----------------------------------------------------------------------
# Module-level wiring hooks (design D1/D7)
# ----------------------------------------------------------------------
_aggregator: Optional[SearchAggregator] = None
_aggregatable_pairs = 0


def configure_aggregator(aggregator: Optional[SearchAggregator]) -> None:
    """Install the process-wide aggregator used by the client dispatchers."""
    global _aggregator
    _aggregator = aggregator


def reset_aggregator() -> None:
    """Remove the process-wide aggregator (tests / shutdown)."""
    global _aggregator
    _aggregator = None


def get_aggregator() -> Optional[SearchAggregator]:
    return _aggregator


def set_aggregatable_pairs(count: int) -> None:
    """Record the planning-time duplicate-pair count (design D7)."""
    global _aggregatable_pairs
    _aggregatable_pairs = int(count)


def get_aggregatable_pairs() -> int:
    return _aggregatable_pairs


def get_aggregation_metrics() -> Dict[str, Any]:
    """Metrics surface for ``PipelineStatus.aggregation_metrics``."""
    if _aggregator is None:
        return {"aggregatable_pairs": _aggregatable_pairs} if _aggregatable_pairs else {}
    return _aggregator.metrics.to_dict()
