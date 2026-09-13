#!/usr/bin/env python3

"""
Frontier-based early-stop detector for API search pagination
(add-search-early-stop).

`sort=indexed&order=desc` returns freshly-indexed files first, so once a run's
trailing window over a partition's result stream is saturated with already
researched links the frontier has been reached and deeper pages contain only
re-runs of the past.  This engine tracks one rolling window per
``(provider, query)`` partition and stops requesting further pages when ALL of
the following hold at a page boundary:

1. known-ratio over the trailing window of ``window`` identities >= ``theta``;
2. at least ``min_pages`` pages were fetched for this partition;
3. the registry trust gate passes (row count, migration marker, no degraded
   run, no registry read errors).

"Known" is not re-implemented here: it is the exact amended gather-skip
conjunction, consumed through :meth:`GatherSkipEngine.classify`.  Everything
else -- discovered-only, foreign coverage, TTL-expired, failed -- is novel.

Rollout is a three-position flag: ``off`` never evaluates, ``shadow`` logs
``would_stop_at`` records and keeps paginating (measuring the false-stop price
as ``novel_after_stop``), and ``on`` enforces the stop.

The detector fails in the safe direction everywhere: any doubt (missing trust,
read error, degraded run) means "keep paginating".
"""

import json
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from storage.gather_skip import (
    DECISION_LOG_FILENAME,
    DEFAULT_TTL_HOURS,
    MODE_OFF,
    MODE_ON,
    MODE_SHADOW,
    VALID_MODES,
    GatherSkipEngine,
)
from storage.registry import REGISTRY_FILENAME, url_hash
from tools.logger import get_logger

logger = get_logger("storage")

DEFAULT_WINDOW = 100
DEFAULT_THETA = 0.9
DEFAULT_MIN_PAGES = 2
DEFAULT_MIN_TRUST = 1000


@dataclass
class PageResult:
    """Outcome of recording one page into a partition's window."""

    provider: str
    query: str
    page: int
    window_size: int
    known_count: int
    ratio: float
    trust_ok: bool
    would_stop: bool
    stopped: bool
    first_stop_page: Optional[int]


@dataclass
class _PartitionTracker:
    """Run-local state for one refined partition."""

    window: "deque" = field(default_factory=deque)
    known: Dict[str, bool] = field(default_factory=dict)
    pages_fetched: int = 0
    max_pages: int = 0
    first_stop_page: Optional[int] = None
    stopped: bool = False
    novel_counted: set = field(default_factory=set)


class EarlyStopEngine:
    """Per-partition saturation tracking + trust-gated stop decisions.

    Thread-safe: the tracker map and counters are guarded by a lock, and
    registry reads use per-thread read-only connections (mirroring
    ``GatherSkipEngine``).  Every public call fails open.
    """

    def __init__(
        self,
        workspace: str,
        mode: str = MODE_OFF,
        window: int = DEFAULT_WINDOW,
        theta: float = DEFAULT_THETA,
        min_pages: int = DEFAULT_MIN_PAGES,
        min_trust: int = DEFAULT_MIN_TRUST,
        ttl_hours: float = DEFAULT_TTL_HOURS,
        registry_path: Optional[str] = None,
        run_id: Optional[str] = None,
        clock=None,
        log_path: Optional[str] = None,
        degraded_probe: Optional[Callable[[], bool]] = None,
        skip_engine: Optional[GatherSkipEngine] = None,
    ):
        self.workspace = str(workspace)
        candidate = str(mode).strip().lower()
        self.mode = candidate if candidate in VALID_MODES else MODE_OFF
        self.window = max(1, int(window))
        self.theta = float(theta)
        self.min_pages = max(1, int(min_pages))
        self.min_trust = max(0, int(min_trust))
        self.ttl_hours = float(ttl_hours)
        self.registry_path = registry_path or os.path.join(self.workspace, REGISTRY_FILENAME)
        self.log_path = log_path or os.path.join(self.workspace, DECISION_LOG_FILENAME)
        self.run_id = run_id
        self._clock = clock or time.time
        self._degraded_probe = degraded_probe

        # Reuse the amended gather-skip conjunction verbatim for "known".  The
        # pipeline injects its shared gather-skip engine so a registry read
        # error observed by either consumer mutes early-stop for the whole run.
        self._owns_skip = skip_engine is None
        self._skip = skip_engine or GatherSkipEngine(
            workspace=self.workspace,
            mode=MODE_ON,
            ttl_hours=self.ttl_hours,
            registry_path=self.registry_path,
            run_id=run_id,
            clock=self._clock,
        )

        self._lock = threading.Lock()
        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._conns: set = set()
        self._log_lock = threading.Lock()
        self._log_handle = None
        self._closed = False

        self._trackers: Dict[Tuple[str, str], _PartitionTracker] = {}
        # The DB-derived half of the trust gate is static for a run (rows only
        # grow, the marker persists), so it is computed once to keep COUNT(*)
        # out of the per-page hot path.
        self._db_trust: Optional[bool] = None
        self.evaluations = 0
        self.early_stop_would_fire = 0
        self.early_stop_fired = 0
        self.novel_after_stop = 0

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """True when the detector may evaluate (shadow/on)."""
        return self.mode != MODE_OFF

    @property
    def enforce(self) -> bool:
        """True when computed stops must actually suppress page tasks."""
        return self.mode == MODE_ON

    # ------------------------------------------------------------------
    # Decision path
    # ------------------------------------------------------------------

    def observe(
        self,
        *,
        provider: str,
        query: str,
        page: int,
        links: Sequence[str],
        patterns_hash: str,
        max_pages: Optional[int] = None,
    ) -> PageResult:
        """Record one page of a partition and evaluate the frontier.

        ``links`` is the raw result page (URLs, in order).  ``max_pages`` is
        supplied on the first page so the tracker can later bound chained page
        generation.
        """
        page = int(page)
        link_list = [link for link in (links or []) if link]
        identities = [url_hash(link) for link in link_list]

        if self.mode == MODE_OFF:
            return PageResult(
                provider=provider,
                query=query,
                page=page,
                window_size=0,
                known_count=0,
                ratio=0.0,
                trust_ok=False,
                would_stop=False,
                stopped=False,
                first_stop_page=None,
            )

        if identities:
            decisions = self._skip.classify(link_list, provider=provider, patterns_hash=patterns_hash)
            known_ids = {decision.url_hash for decision in decisions if decision.skip}
        else:
            known_ids = set()

        payload: Optional[Dict[str, Any]] = None
        with self._lock:
            self.evaluations += 1
            key = (provider, query)
            tracker = self._trackers.get(key)
            if tracker is None:
                tracker = _PartitionTracker()
                self._trackers[key] = tracker

            if max_pages is not None:
                tracker.max_pages = max(tracker.max_pages, int(max_pages))
            tracker.pages_fetched = max(tracker.pages_fetched, page)

            for identity in identities:
                self._push(tracker, identity, identity in known_ids)

            window_size = len(tracker.window)
            known_count = sum(1 for identity in tracker.window if tracker.known.get(identity))
            ratio = (known_count / window_size) if window_size else 0.0
            trust_ok = self._trust_ok()
            would_stop = bool(
                window_size > 0
                and ratio >= self.theta
                and tracker.pages_fetched >= self.min_pages
                and trust_ok
            )

            first_hit = False
            if would_stop and tracker.first_stop_page is None:
                tracker.first_stop_page = page
                first_hit = True
                if self.mode in (MODE_SHADOW, MODE_ON):
                    self.early_stop_would_fire += 1
            if self.mode == MODE_ON and would_stop and not tracker.stopped:
                tracker.stopped = True
                self.early_stop_fired += 1

            # False-stop price: novel identities arriving after the first
            # hypothetical stop point (shadow keeps paginating to measure it).
            if tracker.first_stop_page is not None and page > tracker.first_stop_page:
                for identity in identities:
                    if not tracker.known.get(identity) and identity not in tracker.novel_counted:
                        tracker.novel_counted.add(identity)
                        self.novel_after_stop += 1

            result = PageResult(
                provider=provider,
                query=query,
                page=page,
                window_size=window_size,
                known_count=known_count,
                ratio=ratio,
                trust_ok=trust_ok,
                would_stop=would_stop,
                stopped=bool(self.mode == MODE_ON and tracker.stopped),
                first_stop_page=tracker.first_stop_page,
            )

            if first_hit and self.mode in (MODE_SHADOW, MODE_ON):
                payload = {
                    "type": "early_stop",
                    "ts": time.time(),
                    "run_id": self.run_id,
                    "provider": provider,
                    "query": query,
                    "page": page,
                    "would_stop_at": page,
                    "window_size": window_size,
                    "known_count": known_count,
                    "ratio": ratio,
                    "theta": self.theta,
                    "min_pages": self.min_pages,
                    "trust_ok": trust_ok,
                    "decision": "stop" if result.stopped else "would_stop",
                    "mode": self.mode,
                }

        if payload is not None:
            self._write_log(payload)

        return result

    def max_pages(self, provider: str, query: str) -> int:
        """Return the tracked page cap for a partition (0 when unknown)."""
        with self._lock:
            tracker = self._trackers.get((provider, query))
            return tracker.max_pages if tracker is not None else 0

    def to_stats(self) -> Dict[str, Any]:
        """Flatten per-run counters for run statistics / status display."""
        with self._lock:
            return {
                "mode": self.mode,
                "early_stop_would_fire": self.early_stop_would_fire,
                "early_stop_fired": self.early_stop_fired,
                "novel_after_stop": self.novel_after_stop,
                "evaluations": self.evaluations,
                "partitions": len(self._trackers),
            }

    def close(self) -> None:
        """Close read-only connections, the decision log and the skip engine."""
        self._closed = True
        self._reset_conn()
        with self._conn_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        with self._log_lock:
            if self._log_handle is not None:
                try:
                    self._log_handle.close()
                except Exception:
                    pass
                self._log_handle = None
        if self._owns_skip:
            try:
                self._skip.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Window bookkeeping
    # ------------------------------------------------------------------

    def _push(self, tracker: _PartitionTracker, identity: str, is_known: bool) -> None:
        """Append an identity to the rolling window with in-window dedupe."""
        if identity in tracker.known:
            try:
                tracker.window.remove(identity)
            except ValueError:  # pragma: no cover - defensive
                pass
        tracker.window.append(identity)
        tracker.known[identity] = bool(is_known)
        while len(tracker.window) > self.window:
            evicted = tracker.window.popleft()
            tracker.known.pop(evicted, None)

    # ------------------------------------------------------------------
    # Trust gate / kill-switch
    # ------------------------------------------------------------------

    def _trust_ok(self) -> bool:
        """Trust gate + kill-switch; any doubt fails open to "keep paging"."""
        if self._degraded():
            return False
        probe = getattr(self._skip, "has_read_errors", None)
        if probe is not None and probe():
            return False
        if self._db_trust is None:
            self._db_trust = self._read_db_trust()
        return self._db_trust

    def _read_db_trust(self) -> bool:
        """Durable half of the gate: row count + migration marker.

        Computed at most once per run (cached in ``_db_trust``). Caching
        ``False`` is deliberately conservative: if the registry crosses
        ``min_trust`` mid-run, the gate still reopens only on the next run —
        biasing toward full passes ("first-ever runs against a fresh registry
        always do full passes", proposal). Caching ``True`` is safe because
        link rows only grow and the migration marker never disappears within
        a run; the volatile gates (degraded flag, read errors) are re-checked
        on every evaluation.
        """
        try:
            conn = self._connect()
            row = conn.execute("SELECT COUNT(*) FROM links").fetchone()
            if row is None or int(row[0]) < self.min_trust:
                return False
            marker = conn.execute("SELECT value FROM meta WHERE key = 'migration_complete'").fetchone()
            if marker is None:
                return False
        except Exception:
            # Missing meta table, unreadable registry, locked DB: no stop.
            return False
        return True

    def _degraded(self) -> bool:
        probe = self._degraded_probe
        if probe is None:
            return False
        try:
            return bool(probe())
        except Exception:
            return True

    # ------------------------------------------------------------------
    # Read path / logging
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open (once per thread) a read-only connection to the registry."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            uri = Path(os.path.abspath(self.registry_path)).as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
            self._local.conn = conn
            with self._conn_lock:
                self._conns.add(conn)
        return conn

    def _reset_conn(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._conn_lock:
            self._conns.discard(conn)
        try:
            conn.close()
        except Exception:
            pass

    def _write_log(self, payload: Dict[str, Any]) -> None:
        try:
            with self._log_lock:
                if self._closed:
                    return
                if self._log_handle is None:
                    # Line-buffered: each record is durable on its own line.
                    self._log_handle = open(self.log_path, "a", encoding="utf-8", buffering=1)
                self._log_handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")
        except Exception as exc:  # decision log is observability, never fatal
            logger.debug(f"[early-stop] decision log write failed: {exc}")
