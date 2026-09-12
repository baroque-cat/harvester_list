#!/usr/bin/env python3

"""
Persistent global link registry.

A write-only SQLite ledger (WAL mode) recording every GitHub link the
harvester discovers and gathers, per-provider gather coverage, and a run
journal.  Integration is fail-open: any registry error logs a warning,
marks the run degraded, and the pipeline keeps running exactly as if the
registry were disabled.

This module never influences a pipeline decision in this change; it only
records.  See ``docs/specs/url_canonicalization.md`` for the identity canon.
"""

import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

from tools.logger import get_logger

logger = get_logger("storage")

# Bump when the schema changes; later changes may only ADD columns/tables.
SCHEMA_VERSION = 1
REGISTRY_FILENAME = "registry.sqlite"

# Operations understood by the writer thread.
_OP_DISCOVER = "discover"
_OP_GATHER = "gather"
_OP_COVERAGE = "coverage"
_OP_METADATA = "metadata"

_DDL = """
CREATE TABLE IF NOT EXISTS links(
  url_hash TEXT PRIMARY KEY,
  url TEXT NOT NULL,
  owner TEXT,
  repo TEXT,
  path TEXT,
  first_seen_ts REAL,
  last_seen_ts REAL,
  gathered_ts REAL,
  visit_status TEXT NOT NULL DEFAULT 'discovered',
  transport TEXT,
  query_origin TEXT,
  provider TEXT,
  repo_pushed_at REAL,
  repo_size_kb INTEGER,
  file_commit_date REAL,
  priority REAL
);

CREATE TABLE IF NOT EXISTS link_coverage(
  url_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  patterns_hash TEXT NOT NULL,
  gathered_ts REAL NOT NULL,
  PRIMARY KEY(url_hash, provider, patterns_hash)
);

CREATE TABLE IF NOT EXISTS keys(
  key_hash TEXT PRIMARY KEY,
  provider TEXT,
  key_ref_masked TEXT,
  address TEXT,
  endpoint TEXT,
  status TEXT,
  first_seen_ts REAL,
  last_recheck_ts REAL,
  source_url_hash TEXT
);

CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  started_at REAL,
  finished_at REAL,
  config_digest TEXT,
  degraded INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_links_repo ON links(owner, repo);
CREATE INDEX IF NOT EXISTS idx_links_last_seen ON links(last_seen_ts);
CREATE INDEX IF NOT EXISTS idx_links_provider ON links(provider);
"""

_DISCOVER_SQL = """
INSERT INTO links (url_hash, url, owner, repo, path, first_seen_ts, last_seen_ts,
                   gathered_ts, visit_status, transport, query_origin, provider)
VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'discovered', ?, ?, ?)
ON CONFLICT(url_hash) DO UPDATE SET
  last_seen_ts = MAX(COALESCE(links.last_seen_ts, excluded.last_seen_ts), excluded.last_seen_ts),
  transport = COALESCE(excluded.transport, links.transport),
  query_origin = COALESCE(excluded.query_origin, links.query_origin),
  provider = COALESCE(links.provider, excluded.provider)
"""

_GATHER_SQL = """
INSERT INTO links (url_hash, url, owner, repo, path, first_seen_ts, last_seen_ts,
                   gathered_ts, visit_status, transport, query_origin, provider)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(url_hash) DO UPDATE SET
  gathered_ts = CASE
      WHEN excluded.visit_status = 'gathered_ok' THEN excluded.gathered_ts
      ELSE links.gathered_ts
  END,
  visit_status = CASE
      WHEN excluded.visit_status = 'gathered_ok' THEN 'gathered_ok'
      WHEN links.visit_status = 'gathered_ok' THEN 'gathered_ok'
      ELSE excluded.visit_status
  END,
  last_seen_ts = MAX(COALESCE(links.last_seen_ts, excluded.last_seen_ts), excluded.last_seen_ts),
  transport = COALESCE(excluded.transport, links.transport),
  provider = COALESCE(links.provider, excluded.provider)
"""

_COVERAGE_SQL = """
INSERT INTO link_coverage (url_hash, provider, patterns_hash, gathered_ts)
VALUES (?, ?, ?, ?)
ON CONFLICT(url_hash, provider, patterns_hash) DO UPDATE SET
  gathered_ts = excluded.gathered_ts
"""

# Non-regressing metadata merge: a NULL observation never erases a known value;
# a non-NULL observation replaces the stored one (dates grow monotonically).
# UPDATE-only on purpose: metadata must never fabricate a link row for an
# unknown url_hash (verification finding S2 -- an INSERT would invent
# first_seen_ts phantom links). Unknown hashes simply affect zero rows.
_METADATA_SQL = """
UPDATE links SET
  repo_pushed_at = COALESCE(?, repo_pushed_at),
  repo_size_kb = COALESCE(?, repo_size_kb),
  file_commit_date = COALESCE(?, file_commit_date)
WHERE url_hash = ?
"""

_RUN_UPSERT_SQL = """
INSERT INTO runs (run_id, started_at, finished_at, config_digest, degraded)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(run_id) DO UPDATE SET
  finished_at = COALESCE(excluded.finished_at, runs.finished_at),
  config_digest = COALESCE(excluded.config_digest, runs.config_digest),
  degraded = CASE WHEN runs.degraded > excluded.degraded THEN runs.degraded ELSE excluded.degraded END
"""


def bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Create tables/indexes if missing and record the schema version."""
    conn.executescript(_DDL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def canonical_url(url: str) -> str:
    """Return the canonical identity form of a URL.

    Lowercases scheme and host, discards the query string and fragment, and
    normalises a trailing slash.  Owner/repo/path/ref letter case is preserved
    because GitHub paths and refs are case-sensitive.
    """
    text = (url or "").strip()
    if not text:
        return ""

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()

    path = parts.path
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path.rstrip("/")

    return urlunsplit((scheme, netloc, path, "", ""))


def url_hash(url: str) -> str:
    """SHA-256 hex digest of the canonical URL."""
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()


def parse_github_url(url: str) -> Tuple[str, str, str]:
    """Split a canonical GitHub URL into ``(owner, repo, path)``."""
    segments = [segment for segment in urlsplit(canonical_url(url)).path.split("/") if segment]
    if len(segments) >= 2:
        return segments[0], segments[1], "/".join(segments[2:])
    if len(segments) == 1:
        return segments[0], "", ""
    return "", "", ""


def patterns_hash(
    key_pattern: str = "",
    address_pattern: str = "",
    endpoint_pattern: str = "",
    model_pattern: str = "",
) -> str:
    """Deterministic hash over a provider's effective extraction patterns."""
    payload = json.dumps(
        {
            "key_pattern": key_pattern or "",
            "address_pattern": address_pattern or "",
            "endpoint_pattern": endpoint_pattern or "",
            "model_pattern": model_pattern or "",
        },
        sort_keys=True,
        ensure_ascii=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def patterns_hash_for(patterns: Any) -> str:
    """Hash a :class:`core.models.Patterns`-like object."""
    if patterns is None:
        return patterns_hash()
    if hasattr(patterns, "to_dict"):
        values = patterns.to_dict()
    elif isinstance(patterns, dict):
        values = patterns
    else:
        values = {name: getattr(patterns, name, "") for name in ("key_pattern", "address_pattern", "endpoint_pattern", "model_pattern")}
    return patterns_hash(
        key_pattern=values.get("key_pattern", ""),
        address_pattern=values.get("address_pattern", ""),
        endpoint_pattern=values.get("endpoint_pattern", ""),
        model_pattern=values.get("model_pattern", ""),
    )


def config_digest(config: Any) -> str:
    """Build a parseable digest of the config relevant to result interpretation."""
    providers: List[Dict[str, Any]] = []
    for task in getattr(config, "tasks", []) or []:
        if not getattr(task, "enabled", False):
            continue
        providers.append(
            {
                "name": getattr(task, "name", ""),
                "use_api": bool(getattr(task, "use_api", False)),
                "patterns_hash": patterns_hash_for(getattr(task, "patterns", None)),
            }
        )
    providers.sort(key=lambda item: item["name"])

    registry_config = getattr(config, "registry", None)
    payload = {
        "providers": providers,
        "registry": {"enabled": bool(getattr(registry_config, "enabled", False))},
    }
    return json.dumps(payload, sort_keys=True)


class Registry:
    """Write-only facade over the SQLite registry with a buffered writer thread."""

    def __init__(self, workspace: str, config: Any = None, enabled: Optional[bool] = None, run_id: Optional[str] = None):
        self.workspace = str(workspace)
        self.config = config

        if enabled is None:
            enabled = bool(getattr(config, "enabled", False))
        self.enabled = bool(enabled)

        batch_size = int(getattr(config, "batch_size", 50) or 50)
        flush_interval = float(getattr(config, "flush_interval", 5) or 5)
        queue_size = int(getattr(config, "queue_size", 100_000) or 100_000)
        configured_path = getattr(config, "path", "") or ""

        if configured_path:
            self.path = configured_path if os.path.isabs(configured_path) else os.path.join(self.workspace, configured_path)
        else:
            self.path = os.path.join(self.workspace, REGISTRY_FILENAME)

        self.batch_size = max(1, batch_size)
        self.flush_interval = max(0.05, flush_interval)

        self._queue: "queue.Queue[Tuple[str, Dict[str, Any]]]" = queue.Queue(maxsize=max(1, queue_size))
        self._write_lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._writer: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._available = False
        self._suppress_writes = False
        self._degraded = False
        self._degraded_kinds: set = set()
        self.dropped = 0
        self._metadata_noops = 0
        self._metadata_noop_logged = False

        self.run_id = run_id

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return self._available

    @property
    def degraded(self) -> bool:
        return self._degraded

    def start(self, run_id: Optional[str] = None, config_digest_value: Optional[str] = None) -> bool:
        """Open the database and start the writer thread. Never raises."""
        if not self.enabled:
            return False
        if self._available:
            return True

        try:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)

            self.dropped = 0
            self._metadata_noops = 0
            self._metadata_noop_logged = False
            conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._bootstrap(conn)
            conn.commit()
            self._conn = conn
            self._available = True
        except Exception as exc:  # corrupt file, permission, disk, ...
            self._degrade("open", exc)
            try:
                if "conn" in locals() and conn is not None:
                    conn.close()
            except Exception:
                pass
            self._available = False
            return False

        self._start_writer()
        if run_id is not None:
            self.start_run(run_id=run_id, config_digest=config_digest_value or "")
        return True

    def stop(self) -> None:
        """Drain queued writes, flush, and close. Never raises."""
        if not self.enabled:
            return
        try:
            if self._writer is not None and self._writer.is_alive():
                self._stop_event.set()
                self._writer.join(timeout=max(5.0, self.flush_interval * 2))

            remaining: List[Tuple[str, Dict[str, Any]]] = []
            while True:
                try:
                    remaining.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            if remaining:
                self._flush_guard(remaining)
        except Exception as exc:
            self._degrade("shutdown", exc)

        try:
            if self._conn is not None:
                self._conn.commit()
        except Exception:
            pass
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:
            pass

        self._conn = None
        self._available = False
        self._writer = None

        if self.dropped:
            logger.warning(f"[registry] dropped {self.dropped} operations due to a full queue")

    def get_stats(self) -> Dict[str, Any]:
        """Expose registry counters/metrics (safe to call at any time)."""
        return {
            "path": self.path,
            "enabled": self.enabled,
            "available": self._available,
            "degraded": self._degraded,
            "dropped": self.dropped,
            "metadata_noops": self._metadata_noops,
            "queued": self._queue.qsize(),
        }

    # ------------------------------------------------------------------
    # Write hooks (all fail-open)
    # ------------------------------------------------------------------

    def record_link(
        self,
        url: str,
        transport: str = "",
        query_origin: str = "",
        provider: str = "",
        ts: Optional[float] = None,
    ) -> None:
        """Record a discovery / re-observation of a link."""
        self._enqueue(
            _OP_DISCOVER,
            {
                "url": url,
                "transport": transport or "",
                "query_origin": query_origin or "",
                "provider": provider or "",
                "ts": time.time() if ts is None else float(ts),
            },
        )

    def record_gather(
        self,
        url: str,
        provider: str = "",
        patterns_hash: str = "",
        success: bool = True,
        ts: Optional[float] = None,
        transport: str = "",
    ) -> None:
        """Record a gather outcome; successful gathers also write coverage."""
        self._enqueue(
            _OP_GATHER,
            {
                "url": url,
                "provider": provider or "",
                "patterns_hash": patterns_hash or "",
                "success": bool(success),
                "transport": transport or "",
                "ts": time.time() if ts is None else float(ts),
            },
        )

    def record_coverage(
        self,
        url: str,
        provider: str,
        patterns_hash: str,
        ts: Optional[float] = None,
    ) -> None:
        """Record provider coverage for a successfully gathered link."""
        self._enqueue(
            _OP_COVERAGE,
            {
                "url": url,
                "provider": provider or "",
                "patterns_hash": patterns_hash or "",
                "ts": time.time() if ts is None else float(ts),
            },
        )

    def record_metadata(self, metadata: Dict[str, Any]) -> None:
        """Record non-regressing freshness/size metadata for links.

        ``metadata`` maps URL -> object carrying ``repo_pushed_at``,
        ``repo_size_kb`` and/or ``file_commit_date`` (for example a
        :class:`core.models.LinkMetadata`).  Missing attributes are treated as
        NULL.  Merge policy: NULL never overwrites a known value; a non-NULL
        value replaces the stored one.
        """
        if not metadata:
            return
        items = []
        for url, meta in metadata.items():
            if not url:
                continue
            items.append(
                (
                    url,
                    getattr(meta, "repo_pushed_at", None),
                    getattr(meta, "repo_size_kb", None),
                    getattr(meta, "file_commit_date", None),
                )
            )
        if items:
            self._enqueue(_OP_METADATA, {"items": items})

    # ------------------------------------------------------------------
    # Run journaling (low frequency, written synchronously)
    # ------------------------------------------------------------------

    def start_run(self, run_id: Optional[str] = None, config_digest: str = "") -> str:
        """Insert a run journal row and remember it as the active run."""
        run_id = run_id or str(uuid.uuid4())
        self.run_id = run_id
        if not self.enabled or not self._available or self._conn is None:
            return run_id

        def action(conn: sqlite3.Connection) -> None:
            conn.execute(_RUN_UPSERT_SQL, (run_id, time.time(), None, config_digest or "", 0))

        self._execute(action, "run_start")
        return run_id

    def finish_run(self, run_id: Optional[str] = None, ts: Optional[float] = None) -> None:
        """Mark the active run finished and persist its degraded flag."""
        run_id = run_id or self.run_id
        if run_id is None or not self.enabled or not self._available or self._conn is None:
            return
        finished = time.time() if ts is None else float(ts)
        degraded = 1 if self._degraded else 0

        def action(conn: sqlite3.Connection) -> None:
            conn.execute(_RUN_UPSERT_SQL, (run_id, None, finished, None, degraded))

        self._execute(action, "run_finish")

    def mark_degraded(self) -> None:
        """Flag the active run degraded (no I/O; persisted at finish_run)."""
        self._degraded = True

    def flush(self, timeout: float = 10.0) -> None:
        """Block until queued writes are processed. Never raises."""
        if not self.enabled or not self._available:
            return
        deadline = time.time() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.time() < deadline:
            time.sleep(0.001)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _bootstrap(self, conn: sqlite3.Connection) -> None:
        bootstrap_schema(conn)

    def _start_writer(self) -> None:
        self._stop_event.clear()
        self._writer = threading.Thread(target=self._writer_loop, name="registry-writer", daemon=True)
        self._writer.start()

    def _writer_loop(self) -> None:
        batch: List[Tuple[str, Dict[str, Any]]] = []
        last_flush = time.time()
        while True:
            stopping = self._stop_event.is_set()
            timeout = 0.0 if stopping else min(self.flush_interval, 0.25)
            try:
                batch.append(self._queue.get(timeout=timeout))
            except queue.Empty:
                pass

            due = batch and (
                len(batch) >= self.batch_size
                or (time.time() - last_flush) >= self.flush_interval
                or stopping
            )
            if due:
                self._flush_guard(batch)
                batch = []
                last_flush = time.time()

            if stopping and self._queue.empty() and not batch:
                break

        if batch:
            self._flush_guard(batch)

    def _flush_guard(self, batch: List[Tuple[str, Dict[str, Any]]]) -> None:
        try:
            if self._available and not self._suppress_writes and self._conn is not None:
                self._flush_batch(batch)
        except Exception as exc:
            self._degrade("write", exc)
            self._suppress_writes = True
            self._safe_rollback()
        finally:
            for _ in batch:
                try:
                    self._queue.task_done()
                except ValueError:
                    pass

    def _flush_batch(self, batch: List[Tuple[str, Dict[str, Any]]]) -> None:
        """Execute one batch of operations in a single transaction."""
        discovers = [payload for op, payload in batch if op == _OP_DISCOVER]
        gathers = [payload for op, payload in batch if op == _OP_GATHER]
        coverages = [payload for op, payload in batch if op == _OP_COVERAGE]
        metadatas = [payload["items"] for op, payload in batch if op == _OP_METADATA]

        with self._write_lock:
            conn = self._conn
            if conn is None:
                raise sqlite3.ProgrammingError("registry connection is not open")

            if discovers:
                conn.executemany(_DISCOVER_SQL, [self._discover_row(payload) for payload in discovers])
            if gathers:
                conn.executemany(_GATHER_SQL, [self._gather_row(payload) for payload in gathers])
                successes = [payload for payload in gathers if payload["success"]]
                if successes:
                    conn.executemany(_COVERAGE_SQL, [self._coverage_row(payload) for payload in successes])
            if coverages:
                conn.executemany(_COVERAGE_SQL, [self._coverage_row(payload) for payload in coverages])
            if metadatas:
                rows = [self._metadata_row(item) for items in metadatas for item in items]
                before = conn.total_changes
                conn.executemany(_METADATA_SQL, rows)
                if conn.total_changes == before:
                    self._metadata_noops += len(rows)
                    if not self._metadata_noop_logged:
                        self._metadata_noop_logged = True
                        logger.debug(
                            "[registry] metadata update matched no existing link rows "
                            f"({len(rows)} observations ignored)"
                        )

            conn.commit()

    @staticmethod
    def _discover_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        canon = canonical_url(payload["url"])
        owner, repo, path = parse_github_url(canon)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        ts = payload["ts"]
        return (
            digest,
            canon,
            owner,
            repo,
            path,
            ts,
            ts,
            payload["transport"],
            payload["query_origin"],
            payload["provider"],
        )

    @staticmethod
    def _gather_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        canon = canonical_url(payload["url"])
        owner, repo, path = parse_github_url(canon)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        ts = payload["ts"]
        success = payload["success"]
        return (
            digest,
            canon,
            owner,
            repo,
            path,
            ts,
            ts,
            ts if success else None,
            "gathered_ok" if success else "failed",
            payload["transport"],
            "",
            payload["provider"],
        )

    @staticmethod
    def _coverage_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        canon = canonical_url(payload["url"])
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        return (digest, payload["provider"], payload["patterns_hash"], payload["ts"])

    @staticmethod
    def _metadata_row(item: Tuple[Any, ...]) -> Tuple[Any, ...]:
        url, repo_pushed_at, repo_size_kb, file_commit_date = item
        canon = canonical_url(url)
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()
        # Keyed by url_hash: metadata only updates rows already discovered.
        return (repo_pushed_at, repo_size_kb, file_commit_date, digest)

    def _enqueue(self, op: str, payload: Dict[str, Any]) -> None:
        if not self.enabled or not self._available or self._suppress_writes:
            return
        try:
            self._queue.put_nowait((op, payload))
        except queue.Full:
            self.dropped += 1
            self._degrade("queue_full", RuntimeError("registry queue is full"))

    def _execute(self, action, kind: str) -> None:
        if not self.enabled:
            return
        try:
            with self._write_lock:
                if self._conn is not None:
                    action(self._conn)
                    self._conn.commit()
        except Exception as exc:
            self._degrade(kind, exc)
            self._safe_rollback()

    def _safe_rollback(self) -> None:
        try:
            if self._conn is not None:
                self._conn.rollback()
        except Exception:
            pass

    def _degrade(self, kind: str, exc: Exception) -> None:
        self._degraded = True
        if kind not in self._degraded_kinds:
            self._degraded_kinds.add(kind)
            logger.warning(f"[registry] {kind} failed; degrading run: {exc}")


# ----------------------------------------------------------------------
# Process-wide singleton used by the pipeline
# ----------------------------------------------------------------------

_registry: Optional[Registry] = None


def init_registry(workspace: str, config: Any = None, enabled: Optional[bool] = None) -> Registry:
    """Create and install the process-wide registry instance."""
    global _registry
    _registry = Registry(workspace, config=config, enabled=enabled)
    return _registry


def get_registry() -> Registry:
    """Return the active registry, or a safe disabled no-op instance."""
    if _registry is None:
        return Registry(workspace=".", enabled=False)
    return _registry


def shutdown_registry() -> None:
    """Stop and drop the process-wide registry instance."""
    global _registry
    if _registry is not None:
        _registry.stop()
        _registry = None
