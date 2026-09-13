#!/usr/bin/env python3

"""
Periodic key re-check driver (add-key-ledger).

``RecheckManager`` selects ledger keys whose per-status TTL has expired and
enqueues ordinary ``CheckTask``s through the pipeline, so all existing provider
rate limits, token buckets and cooldowns apply unchanged (design D5).

Secret hygiene forces one indirection: the registry stores only hashes and
masked references, never a plaintext key.  The manager therefore needs a
resolver that recovers the plaintext from the result shards (the legitimate
home of harvested keys) in order to build a ``CheckTask``.  The default
:class:`ShardKeyResolver` scans ``providers/<provider>`` shard files; tests
inject a fake resolver instead.
"""

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from core.models import CheckTask, Service
from manager.base import PeriodicTaskManager
from storage.key_ledger import DEFAULT_TTL_HOURS, STATUSES, key_hash
from tools.logger import get_logger

logger = get_logger("manager")

REGISTRY_FILENAME = "registry.sqlite"
_SECONDS_PER_HOUR = 3600.0

# Priority order (design D5): wait_check first (temporary refusal, retry soon),
# then valid, then no_quota, then everything else (invalid).
_PRIORITY_ORDER = ("wait_check", "valid", "no_quota", "invalid")

_SELECT_EXPIRED_SQL = """
SELECT key_hash, provider, address, endpoint, status, last_recheck_ts, source_url_hash
FROM keys
WHERE status IS NOT NULL
  AND (
    last_recheck_ts IS NULL
    OR (? - last_recheck_ts) > (
      CASE status
        WHEN 'wait_check' THEN ?
        WHEN 'valid' THEN ?
        WHEN 'no_quota' THEN ?
        WHEN 'invalid' THEN ?
        ELSE ?
      END
    )
  )
ORDER BY CASE status
           WHEN 'wait_check' THEN 0
           WHEN 'valid' THEN 1
           WHEN 'no_quota' THEN 2
           ELSE 3
         END,
         last_recheck_ts ASC
LIMIT ?
"""


class ShardKeyResolver:
    """Recover a plaintext :class:`Service` from a workspace's result shards.

    The registry never stores plaintext, so the re-check driver must resolve it
    from where harvested keys legitimately live.  Resolution is lazy and
    on-demand: the provider directory is scanned for a ``Service`` whose
    canonical identity (using the ledger row's address/endpoint) matches.
    """

    def __init__(self, workspace: str):
        self.workspace = str(workspace)

    def resolve(self, row: Dict[str, Any]) -> Optional[Service]:
        provider = row["provider"] or ""
        target = row["key_hash"]
        address = row["address"] or ""
        endpoint = row["endpoint"] or ""
        directory = os.path.join(self.workspace, "providers", provider)
        if not os.path.isdir(directory):
            return None
        for path in self._candidate_files(directory):
            for service in _iter_services(path):
                if key_hash(provider, service.key, address, service.endpoint) == target:
                    return service
        return None

    @staticmethod
    def _candidate_files(directory: str):
        for root, _dirs, files in os.walk(directory):
            for name in sorted(files):
                if name.endswith((".ndjson", ".txt")):
                    yield os.path.join(root, name)


def _iter_services(path: str):
    """Yield :class:`Service` objects from a shard/legacy file (tolerant parse)."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                service = _parse_service_line(line)
                if service is not None and service.key:
                    yield service
    except OSError:
        return


def _parse_service_line(line: str) -> Optional[Service]:
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        # Legacy plain-key line.
        return Service(key=line)
    if not isinstance(record, dict):
        return Service(key=str(record))
    service_fields = ("key", "address", "endpoint", "model")
    if "value" in record and not any(field in record for field in service_fields):
        return Service.deserialize(str(record["value"]))
    return Service.from_dict({field: str(record.get(field, "") or "") for field in service_fields})


class RecheckManager(PeriodicTaskManager):
    """Periodic driver that re-enqueues TTL-expired ledger keys."""

    def __init__(
        self,
        workspace: str,
        registry_path: Optional[str] = None,
        ttl_hours: Optional[Dict[str, float]] = None,
        enabled: bool = False,
        interval_hours: float = 6.0,
        batch_size: int = 50,
        enqueue: Optional[Callable[[CheckTask], Any]] = None,
        resolve: Optional[Callable[[Dict[str, Any]], Optional[Service]]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        super().__init__("RecheckManager", interval=max(0.1, float(interval_hours) * _SECONDS_PER_HOUR))
        self.workspace = str(workspace)
        self.registry_path = registry_path or os.path.join(self.workspace, REGISTRY_FILENAME)
        self.ttl_hours: Dict[str, float] = {status: float(DEFAULT_TTL_HOURS[status]) for status in STATUSES}
        if ttl_hours:
            for status, hours in ttl_hours.items():
                try:
                    self.ttl_hours[str(status)] = float(hours)
                except (TypeError, ValueError):
                    continue
        self.enabled = bool(enabled)
        self.batch_size = max(1, int(batch_size))
        self._enqueue = enqueue
        # Accept either a callable ``row -> Service|None`` or an object with a
        # ``.resolve`` method (ShardKeyResolver); found via staging run.
        resolver = resolve if resolve is not None else ShardKeyResolver(self.workspace)
        self._resolve = resolver.resolve if hasattr(resolver, "resolve") else resolver
        self._clock = clock or time.time

        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._conns: set = set()
        self._stat_lock = threading.Lock()

        self.rechecks_enqueued = 0
        self.rechecks_refused = 0
        self.unresolved = 0
        self.selection_errors = 0

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    def to_stats(self) -> Dict[str, Any]:
        with self._stat_lock:
            return {
                "enabled": self.enabled,
                "rechecks_enqueued": self.rechecks_enqueued,
                "rechecks_refused": self.rechecks_refused,
                "unresolved": self.unresolved,
                "selection_errors": self.selection_errors,
            }

    def close(self) -> None:
        self._reset_conn()
        with self._conn_lock:
            conns = list(self._conns)
            self._conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Periodic task
    # ------------------------------------------------------------------

    def _execute_periodic_task(self) -> None:
        if not self.enabled:
            return
        try:
            rows = self.select_expired()
        except Exception as exc:
            with self._stat_lock:
                self.selection_errors += 1
            logger.warning(f"[recheck] ledger selection failed (fail-open): {exc}")
            self._reset_conn()
            return

        for row in rows:
            service = self._resolve_row(row)
            if service is None:
                with self._stat_lock:
                    self.unresolved += 1
                continue
            task = CheckTask(
                provider=row["provider"] or "",
                service=service,
                source_url_hash=row["source_url_hash"] or "",
            )
            try:
                refused = False
                if self._enqueue is not None:
                    # A bool-returning entry point (Pipeline.put_task wrapper)
                    # reports False when the stage deduped/refused the task.
                    refused = self._enqueue(task) is False
                with self._stat_lock:
                    if refused:
                        self.rechecks_refused += 1
                    else:
                        self.rechecks_enqueued += 1
            except Exception as exc:
                logger.warning(f"[recheck] failed to enqueue re-check task (fail-open): {exc}")

    def select_expired(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Return TTL-expired ledger rows in priority order (oldest first)."""
        moment = self._clock() if now is None else float(now)
        conn = self._connect()
        ttl = {status: self.ttl_hours.get(status, 0.0) * _SECONDS_PER_HOUR for status in STATUSES}
        params = [
            moment,
            ttl["wait_check"],
            ttl["valid"],
            ttl["no_quota"],
            ttl["invalid"],
            0.0,  # unknown status => immediately expired
            self.batch_size,
        ]
        rows = conn.execute(_SELECT_EXPIRED_SQL, params).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_row(self, row: Dict[str, Any]) -> Optional[Service]:
        try:
            return self._resolve(row)
        except Exception as exc:  # fail-open: skip this key, keep draining
            logger.debug(f"[recheck] key resolution failed (fail-open): {exc}")
            return None

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            uri = Path(os.path.abspath(self.registry_path)).as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5.0)
            conn.row_factory = sqlite3.Row
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
