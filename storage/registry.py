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

from storage.priority import (
    PriorityWeights,
    apply_scores,
    repos_for_url_hashes,
    score_many,
    sweep as sweep_priorities_sql,
    weights_from_config,
)
from tools.logger import get_logger

logger = get_logger("storage")

# Bump when the schema changes; later changes may only ADD columns/tables.
SCHEMA_VERSION = 6
REGISTRY_FILENAME = "registry.sqlite"

# Operations understood by the writer thread.
_OP_DISCOVER = "discover"
_OP_GATHER = "gather"
_OP_COVERAGE = "coverage"
_OP_METADATA = "metadata"
_OP_REPO_UPSERT = "repo_upsert"
_OP_REPO_TOUCH = "repo_touch"
_OP_REPO_GONE = "repo_gone"
_OP_REPO_LINKS = "repo_links"
_OP_KEY_UPSERT = "key_upsert"
_OP_KEY_OBSERVE = "key_observe"
_OP_KEY_SOURCE = "key_source"
_OP_FILE_UPSERT = "file_upsert"
_OP_FILE_TOUCH = "file_touch"
_OP_FILE_GONE = "file_gone"
_OP_FILE_LINKS = "file_links"

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
  priority REAL,
  etag TEXT,
  etag_transport TEXT
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
  last_seen_ts REAL,
  source_url_hash TEXT
);

CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  started_at REAL,
  finished_at REAL,
  config_digest TEXT,
  degraded INTEGER NOT NULL DEFAULT 0
);

-- Repo-level metadata cache (add-repo-meta-enrichment D2).  Denormalized
-- freshness is deliberately kept out of `links` so the acquisition channel is
-- additive and independently migratable.
CREATE TABLE IF NOT EXISTS repos(
  owner TEXT NOT NULL,
  repo TEXT NOT NULL,
  pushed_at REAL,
  size_kb INTEGER,
  default_branch TEXT,
  etag TEXT,
  fetched_at REAL NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(owner, repo)
);

-- Additive key/value markers (add-search-early-stop D4).  The trust gate reads
-- `migration_complete=<ts>` written by tools/registry_migrate.py to prove the
-- one-time shard migration has run before early-stop may enforce.
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT
);

-- Per-file freshness evidence cache (add-file-commit-dating D1/D2).  Keyed by
-- (owner, repo, path) with a resolved_sha for cache invalidation.  Writes go
-- through the single registry writer thread; always fail-open.
CREATE TABLE IF NOT EXISTS file_meta(
  owner TEXT NOT NULL,
  repo TEXT NOT NULL,
  path TEXT NOT NULL,
  resolved_sha TEXT,
  committed_date REAL,
  etag TEXT,
  fetched_at REAL NOT NULL,
  gone INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(owner, repo, path)
);

CREATE INDEX IF NOT EXISTS idx_links_repo ON links(owner, repo);
CREATE INDEX IF NOT EXISTS idx_links_last_seen ON links(last_seen_ts);
CREATE INDEX IF NOT EXISTS idx_links_provider ON links(provider);
CREATE INDEX IF NOT EXISTS idx_keys_recheck ON keys(status, last_recheck_ts);
CREATE INDEX IF NOT EXISTS idx_file_meta_fetched ON file_meta(fetched_at);

-- Additive child table for multi-source key attribution (P22 D1).  Records
-- every (key_hash, url_hash) pair a key has been observed with, so the link
-- to an earlier source file is never lost when the key is re-found elsewhere.
CREATE TABLE IF NOT EXISTS key_sources(
  key_hash TEXT NOT NULL,
  url_hash TEXT NOT NULL,
  first_seen_ts REAL,
  last_seen_ts REAL,
  PRIMARY KEY(key_hash, url_hash)
);
CREATE INDEX IF NOT EXISTS idx_key_sources_url ON key_sources(url_hash);

-- Dated view joining key_sources to links for per-file freshness (P22 D4).
CREATE VIEW IF NOT EXISTS key_sources_dated AS
SELECT ks.key_hash, ks.url_hash, l.url, l.owner, l.repo, l.path,
       l.file_commit_date, l.visit_status, ks.first_seen_ts, ks.last_seen_ts
FROM key_sources ks JOIN links l ON l.url_hash = ks.url_hash;
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
                   gathered_ts, visit_status, transport, query_origin, provider,
                   etag, etag_transport)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
  provider = COALESCE(links.provider, excluded.provider),
  etag = CASE
      WHEN excluded.etag IS NOT NULL AND excluded.etag != '' THEN excluded.etag
      ELSE links.etag
  END,
  etag_transport = CASE
      WHEN excluded.etag IS NOT NULL AND excluded.etag != '' THEN excluded.etag_transport
      ELSE links.etag_transport
  END
"""

_COVERAGE_SQL = """
INSERT INTO link_coverage (url_hash, provider, patterns_hash, gathered_ts)
VALUES (?, ?, ?, ?)
ON CONFLICT(url_hash, provider, patterns_hash) DO UPDATE SET
  gathered_ts = excluded.gathered_ts
"""

# Ledger upsert after a real provider check (add-key-ledger D1/D2).  Identity is
# the primary key, so an existing row keeps its original first_seen_ts while the
# status and last_recheck_ts advance; last_seen_ts never regresses.  Only the
# hash and masked reference are persisted -- never the plaintext secret.
_KEY_UPSERT_SQL = """
INSERT INTO keys (key_hash, provider, key_ref_masked, address, endpoint, status,
                  first_seen_ts, last_recheck_ts, last_seen_ts, source_url_hash)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(key_hash) DO UPDATE SET
  provider = COALESCE(NULLIF(excluded.provider, ''), keys.provider),
  key_ref_masked = COALESCE(NULLIF(excluded.key_ref_masked, ''), keys.key_ref_masked),
  address = COALESCE(NULLIF(excluded.address, ''), keys.address),
  endpoint = COALESCE(NULLIF(excluded.endpoint, ''), keys.endpoint),
  status = excluded.status,
  last_recheck_ts = excluded.last_recheck_ts,
  last_seen_ts = MAX(COALESCE(keys.last_seen_ts, excluded.last_seen_ts), excluded.last_seen_ts),
  -- Same discipline as _DISCOVER_SQL's provider = COALESCE(links.provider, excluded.provider)
  -- (first wins forever) and as the immutable first_seen_ts; deterministic
  -- regardless of check order, so links.priority stays reproducible (P22 D2).
  source_url_hash = COALESCE(NULLIF(keys.source_url_hash, ''), NULLIF(excluded.source_url_hash, ''))
"""

# Observation-only update for a skipped check (add-key-ledger D4): the status
# and last_recheck_ts are intentionally left untouched; only recency advances.
# UPDATE-only: an unknown hash affects zero rows and fabricates nothing.
_KEY_OBSERVE_SQL = """
UPDATE keys SET last_seen_ts = MAX(COALESCE(last_seen_ts, ?), ?)
WHERE key_hash = ?
"""

# Key-source attribution upsert (P22 D3).  first_seen_ts is immutable on
# conflict — mirrors the links.first_seen_ts discipline.  last_seen_ts uses
# MAX to never regress.
_KEY_SOURCE_SQL = """
INSERT INTO key_sources (key_hash, url_hash, first_seen_ts, last_seen_ts)
VALUES (?, ?, ?, ?)
ON CONFLICT(key_hash, url_hash) DO UPDATE SET
  last_seen_ts = MAX(COALESCE(key_sources.last_seen_ts, excluded.last_seen_ts), excluded.last_seen_ts)
"""

# Non-regressing metadata merge: a NULL observation never erases a known value;
# a non-NULL observation replaces the stored one only when strictly newer
# (design D7: monotonic maximum).  repo_size_kb keeps COALESCE because size is
# not a timestamp.
# UPDATE-only on purpose: metadata must never fabricate a link row for an
# unknown url_hash (verification finding S2 -- an INSERT would invent
# first_seen_ts phantom links). Unknown hashes simply affect zero rows.
_METADATA_SQL = """
UPDATE links SET
  repo_pushed_at = CASE WHEN ? IS NULL THEN repo_pushed_at WHEN repo_pushed_at IS NULL THEN ? ELSE MAX(repo_pushed_at, ?) END,
  repo_size_kb = COALESCE(?, repo_size_kb),
  file_commit_date = CASE WHEN ? IS NULL THEN file_commit_date WHEN file_commit_date IS NULL THEN ? ELSE MAX(file_commit_date, ?) END
WHERE url_hash = ?
"""

# Repo-scoped metadata merge (add-repo-meta-enrichment D3/D2).  Same
# non-regression contract as `_METADATA_SQL` but keyed by (owner, repo) so a
# single successful fetch propagates to every link of the repository.
# Date columns use a monotonic maximum (design D7): a NULL never overwrites a
# known value, and a non-NULL replaces only when strictly newer.
# UPDATE-only on purpose: unknown repositories affect zero rows and never
# fabricate phantom link rows.
_REPO_LINKS_SQL = """
UPDATE links SET
  repo_pushed_at = CASE WHEN ? IS NULL THEN repo_pushed_at WHEN repo_pushed_at IS NULL THEN ? ELSE MAX(repo_pushed_at, ?) END,
  repo_size_kb = COALESCE(?, repo_size_kb)
WHERE owner = ? AND repo = ?
"""

# Full-replacement upsert for a 200 fetch (add-repo-meta-enrichment D3).  A 200
# is a complete repository object, so absent (trimmed) fields deliberately
# become NULL -- the opportunistic contract.
_REPO_UPSERT_SQL = """
INSERT INTO repos (owner, repo, pushed_at, size_kb, default_branch, etag, fetched_at, gone)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(owner, repo) DO UPDATE SET
  pushed_at = excluded.pushed_at,
  size_kb = excluded.size_kb,
  default_branch = excluded.default_branch,
  etag = excluded.etag,
  fetched_at = excluded.fetched_at,
  gone = excluded.gone
"""

# Touch-only refresh for a 304 (add-repo-meta-enrichment D3): bump fetched_at
# and, defensively, refresh the etag without touching any value column.
_REPO_TOUCH_SQL = """
UPDATE repos SET
  fetched_at = COALESCE(?, fetched_at),
  etag = COALESCE(?, etag)
WHERE owner = ? AND repo = ?
"""

# Takedown marker (add-repo-meta-enrichment D8): records gone=1 while keeping
# any previously known values, so a temporary 404 never erases evidence.
_REPO_GONE_SQL = """
INSERT INTO repos (owner, repo, pushed_at, size_kb, default_branch, etag, fetched_at, gone)
VALUES (?, ?, NULL, NULL, NULL, NULL, ?, 1)
ON CONFLICT(owner, repo) DO UPDATE SET
  fetched_at = excluded.fetched_at,
  gone = 1
"""

# Per-file cache upsert (add-file-commit-dating D1/D2).  committed_date uses
# a monotonic maximum so a stale re-propagation never regresses the stored
# value (design D7).
_FILE_UPSERT_SQL = """
INSERT INTO file_meta (owner, repo, path, resolved_sha, committed_date, etag, fetched_at, gone)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(owner, repo, path) DO UPDATE SET
  resolved_sha = excluded.resolved_sha,
  committed_date = CASE
      WHEN excluded.committed_date IS NULL THEN file_meta.committed_date
      WHEN file_meta.committed_date IS NULL THEN excluded.committed_date
      ELSE MAX(file_meta.committed_date, excluded.committed_date)
  END,
  etag = excluded.etag,
  fetched_at = excluded.fetched_at,
  gone = excluded.gone
"""

# Touch-only refresh for a file cache 304: bump fetched_at and optionally
# refresh the etag without touching committed_date.
_FILE_TOUCH_SQL = """
UPDATE file_meta SET
  fetched_at = COALESCE(?, fetched_at),
  etag = COALESCE(?, etag)
WHERE owner = ? AND repo = ? AND path = ?
"""

# Takedown marker for a file path: records gone=1 while keeping any previously
# known values, so a temporary 404 never erases evidence.
_FILE_GONE_SQL = """
INSERT INTO file_meta (owner, repo, path, resolved_sha, committed_date, etag, fetched_at, gone)
VALUES (?, ?, ?, NULL, NULL, NULL, ?, 1)
ON CONFLICT(owner, repo, path) DO UPDATE SET
  fetched_at = excluded.fetched_at,
  gone = 1
"""

# Propagate a known committed_date to every links row of (owner, repo, path).
# Monotonic maximum (design D7): a NULL never overwrites, a non-NULL replaces
# only when strictly newer.  UPDATE-only: unknown paths affect zero rows.
_FILE_LINKS_SQL = """
UPDATE links SET
  file_commit_date = CASE
      WHEN ? IS NULL THEN links.file_commit_date
      WHEN links.file_commit_date IS NULL THEN ?
      ELSE MAX(links.file_commit_date, ?)
  END
WHERE owner = ? AND repo = ? AND path = ?
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
    _migrate_schema(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for registries created by earlier versions.

    ``CREATE TABLE IF NOT EXISTS`` does not add columns to an existing table, so
    databases created at schema_version < 4 need an explicit, idempotent ALTER
    guard (design D6: additive columns only).  schema_version < 5 adds the
    ``etag`` and ``etag_transport`` columns to ``links``; the ``file_meta``
    table needs no guard because ``CREATE TABLE IF NOT EXISTS`` in ``_DDL``
    already handles it.

    schema_version < 6 (P22 D6): backfill ``key_sources`` from existing
    ``keys`` rows that have a non-empty ``source_url_hash``.  Gated on the
    absence of a ``meta`` marker so reopening is O(1) and the original
    provenance is preserved.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(keys)")}
    if "last_seen_ts" not in columns:
        conn.execute("ALTER TABLE keys ADD COLUMN last_seen_ts REAL")

    # schema_version < 5: add etag/etag_transport to links (design D10).
    link_columns = {row[1] for row in conn.execute("PRAGMA table_info(links)")}
    if "etag" not in link_columns:
        conn.execute("ALTER TABLE links ADD COLUMN etag TEXT")
    if "etag_transport" not in link_columns:
        conn.execute("ALTER TABLE links ADD COLUMN etag_transport TEXT")

    # schema_version < 6 (P22 D6): backfill key_sources from keys.
    marker_row = conn.execute(
        "SELECT value FROM meta WHERE key = 'key_sources_backfill'"
    ).fetchone()
    if marker_row is None:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO key_sources (key_hash, url_hash, first_seen_ts, last_seen_ts) "
            "SELECT key_hash, source_url_hash, first_seen_ts, last_seen_ts FROM keys "
            "WHERE source_url_hash IS NOT NULL AND source_url_hash <> ''"
        )
        inserted = conn.total_changes - before
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('key_sources_backfill', ?)",
            (f"{inserted}@{time.time()}",)
        )


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

    def __init__(
        self,
        workspace: str,
        config: Any = None,
        enabled: Optional[bool] = None,
        run_id: Optional[str] = None,
        prioritization: Any = None,
    ):
        self.workspace = str(workspace)
        self.config = config

        if enabled is None:
            enabled = bool(getattr(config, "enabled", False))
        self.enabled = bool(enabled)

        # Scoring parameters for the denormalized links.priority column.
        self.priority_weights: PriorityWeights = weights_from_config(prioritization)

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
        self._dropped_lock = threading.Lock()
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
        """Expose registry counters/metrics (safe to call at any time).

        Includes three ``key_sources`` aggregates (P22 D7): ``key_source_rows``
        (COUNT of ``key_sources``), ``multi_source_keys`` (COUNT of key_hash
        groups having >1 row), and ``key_sources_backfilled`` (the integer
        parsed from the ``meta`` marker prefix before ``@``; 0 when absent or
        unparsable).  Cost: two small aggregates per status render (~30 s
        cadence).  Read through a dedicated read-only connection following the
        house pattern of ``get_link_fetch_state()``; ANY exception degrades to
        zeros and never raises into a caller.
        """
        base = {
            "path": self.path,
            "enabled": self.enabled,
            "available": self._available,
            "degraded": self._degraded,
            "dropped": self.dropped,
            "metadata_noops": self._metadata_noops,
            "queued": self._queue.qsize(),
        }
        # P22 D7: key_sources counters (fail-open, read-only).
        if not self.enabled or not self._available:
            base["key_source_rows"] = 0
            base["multi_source_keys"] = 0
            base["key_sources_backfilled"] = 0
            return base
        try:
            uri = f"file:{self.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT "
                    "(SELECT COUNT(*) FROM key_sources) AS key_source_rows, "
                    "(SELECT COUNT(*) FROM (SELECT key_hash FROM key_sources GROUP BY key_hash HAVING COUNT(*) > 1)) AS multi_source_keys"
                ).fetchone()
                key_source_rows = row["key_source_rows"] if row else 0
                multi_source_keys = row["multi_source_keys"] if row else 0

                marker_row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'key_sources_backfill'"
                ).fetchone()
                key_sources_backfilled = 0
                if marker_row and marker_row["value"]:
                    try:
                        key_sources_backfilled = int(str(marker_row["value"]).split("@")[0])
                    except (ValueError, IndexError):
                        key_sources_backfilled = 0
            finally:
                conn.close()
            base["key_source_rows"] = key_source_rows
            base["multi_source_keys"] = multi_source_keys
            base["key_sources_backfilled"] = key_sources_backfilled
        except Exception:  # pragma: no cover - defensive fail-open
            base["key_source_rows"] = 0
            base["multi_source_keys"] = 0
            base["key_sources_backfilled"] = 0
        return base

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
        etag: Optional[str] = None,
        etag_transport: str = "",
    ) -> None:
        """Record a gather outcome; successful gathers also write coverage.

        ``etag`` and ``etag_transport`` carry the conditional-revalidation
        token returned by the transport (design D6/link-registry-S10).  An
        empty/None ``etag`` leaves any previously stored token untouched.
        """
        self._enqueue(
            _OP_GATHER,
            {
                "url": url,
                "provider": provider or "",
                "patterns_hash": patterns_hash or "",
                "success": bool(success),
                "transport": transport or "",
                "ts": time.time() if ts is None else float(ts),
                "etag": etag,
                "etag_transport": etag_transport or "",
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
        NULL.  Merge policy (design D7): NULL never overwrites a known value;
        a non-NULL date replaces the stored one only when strictly newer
        (monotonic maximum).  ``repo_size_kb`` keeps COALESCE semantics
        because size is not a timestamp.
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

    def record_repo_upsert(
        self,
        owner: str,
        repo: str,
        pushed_at: Optional[float] = None,
        size_kb: Optional[int] = None,
        default_branch: Optional[str] = None,
        etag: Optional[str] = None,
        ts: Optional[float] = None,
        gone: int = 0,
    ) -> None:
        """Replace the cached repository metadata from a full 200 response."""
        self._enqueue(
            _OP_REPO_UPSERT,
            {
                "owner": owner or "",
                "repo": repo or "",
                "pushed_at": pushed_at,
                "size_kb": size_kb,
                "default_branch": default_branch,
                "etag": etag,
                "ts": time.time() if ts is None else float(ts),
                "gone": 1 if gone else 0,
            },
        )

    def record_repo_touch(self, owner: str, repo: str, ts: Optional[float] = None, etag: Optional[str] = None) -> None:
        """Touch-only refresh after a 304 (fetched_at bump, values preserved)."""
        self._enqueue(
            _OP_REPO_TOUCH,
            {
                "owner": owner or "",
                "repo": repo or "",
                "ts": time.time() if ts is None else float(ts),
                "etag": etag,
            },
        )

    def record_repo_gone(self, owner: str, repo: str, ts: Optional[float] = None) -> None:
        """Record a takedown (404) without erasing previously known values."""
        self._enqueue(
            _OP_REPO_GONE,
            {
                "owner": owner or "",
                "repo": repo or "",
                "ts": time.time() if ts is None else float(ts),
            },
        )

    def record_repo_link_metadata(
        self,
        owner: str,
        repo: str,
        pushed_at: Optional[float] = None,
        size_kb: Optional[int] = None,
    ) -> None:
        """Propagate repo metadata to every link of ``(owner, repo)`` (UPDATE-only)."""
        self._enqueue(
            _OP_REPO_LINKS,
            {
                "owner": owner or "",
                "repo": repo or "",
                "pushed_at": pushed_at,
                "size_kb": size_kb,
            },
        )

    def record_file_upsert(
        self,
        owner: str,
        repo: str,
        path: str,
        resolved_sha: Optional[str] = None,
        committed_date: Optional[float] = None,
        etag: Optional[str] = None,
        ts: Optional[float] = None,
        gone: int = 0,
    ) -> None:
        """Replace the cached file metadata from a full response (design D1/D2)."""
        self._enqueue(
            _OP_FILE_UPSERT,
            {
                "owner": owner or "",
                "repo": repo or "",
                "path": path or "",
                "resolved_sha": resolved_sha,
                "committed_date": committed_date,
                "etag": etag,
                "ts": time.time() if ts is None else float(ts),
                "gone": 1 if gone else 0,
            },
        )

    def record_file_touch(
        self, owner: str, repo: str, path: str, ts: Optional[float] = None, etag: Optional[str] = None
    ) -> None:
        """Touch-only refresh after a 304 (fetched_at bump, values preserved)."""
        self._enqueue(
            _OP_FILE_TOUCH,
            {
                "owner": owner or "",
                "repo": repo or "",
                "path": path or "",
                "ts": time.time() if ts is None else float(ts),
                "etag": etag,
            },
        )

    def record_file_gone(self, owner: str, repo: str, path: str, ts: Optional[float] = None) -> None:
        """Record a not-found target without erasing previously known values."""
        self._enqueue(
            _OP_FILE_GONE,
            {
                "owner": owner or "",
                "repo": repo or "",
                "path": path or "",
                "ts": time.time() if ts is None else float(ts),
            },
        )

    def record_file_link_metadata(
        self, owner: str, repo: str, path: str, committed_date: Optional[float] = None
    ) -> None:
        """Propagate a known committed_date to every link of (owner, repo, path).

        UPDATE-only: never fabricates link rows.  Monotonic maximum (design D7).
        """
        self._enqueue(
            _OP_FILE_LINKS,
            {
                "owner": owner or "",
                "repo": repo or "",
                "path": path or "",
                "committed_date": committed_date,
            },
        )

    def record_key(
        self,
        key_hash: str,
        provider: str = "",
        key_ref_masked: str = "",
        address: str = "",
        endpoint: str = "",
        status: str = "",
        source_url_hash: str = "",
        ts: Optional[float] = None,
    ) -> None:
        """Upsert a ledger row after a real provider check (add-key-ledger).

        ``key_ref_masked`` must already be a masked reference; callers must
        never pass a plaintext secret (secret hygiene, key-ledger-S9).  On an
        existing row ``first_seen_ts`` is preserved and ``last_recheck_ts`` /
        ``last_seen_ts`` advance with the check.

        Additionally enqueues a ``key_sources`` row when both ``key_hash`` and
        ``source_url_hash`` are non-empty (P22 D3).
        """
        if not key_hash:
            return
        moment = time.time() if ts is None else float(ts)
        self._enqueue(
            _OP_KEY_UPSERT,
            {
                "key_hash": key_hash,
                "provider": provider or "",
                "key_ref_masked": key_ref_masked or "",
                "address": address or "",
                "endpoint": endpoint or "",
                "status": status or "",
                "source_url_hash": source_url_hash or "",
                "ts": moment,
            },
        )
        # P22 D3: also record the (key, source) pair for multi-source tracking.
        if source_url_hash:
            self._enqueue(
                _OP_KEY_SOURCE,
                {
                    "key_hash": key_hash,
                    "url_hash": source_url_hash,
                    "ts": moment,
                },
            )

    def record_key_observation(self, key_hash: str, ts: Optional[float] = None, source_url_hash: str = "") -> None:
        """Advance ``last_seen_ts`` for a skipped observation (UPDATE-only).

        When ``source_url_hash`` is non-empty, also enqueues a ``key_sources``
        row so the observation hook can link a new file even when the check
        was skipped (P22 D3).
        """
        if not key_hash:
            return
        moment = time.time() if ts is None else float(ts)
        self._enqueue(
            _OP_KEY_OBSERVE,
            {
                "key_hash": key_hash,
                "ts": moment,
            },
        )
        # P22 D3: also record the (key, source) pair when known.
        if source_url_hash:
            self._enqueue(
                _OP_KEY_SOURCE,
                {
                    "key_hash": key_hash,
                    "url_hash": source_url_hash,
                    "ts": moment,
                },
            )

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
        # Hard convergence guarantee (design D3): recompute every repository's
        # priority before journaling the finish, healing any mutations that
        # bypassed dirty-marking (migrations, manual edits).
        self.sweep_priorities()
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

    def sweep_priorities(self, weights: Any = None, now: Optional[float] = None) -> int:
        """Recompute and persist priority for every repository (fail-open).

        Drains pending writes first so the sweep observes a settled ledger, then
        runs the full ``DISTINCT owner, repo`` recomputation through the writer
        connection.  Returns the number of repositories scored (0 when the
        registry is disabled/unavailable).
        """
        if not self.enabled or not self._available or self._conn is None:
            return 0
        try:
            self.flush(max(10.0, self.flush_interval * 2))
        except Exception:
            pass
        resolved = weights_from_config(weights) if weights is not None else self.priority_weights
        result = {"count": 0}

        def action(conn: sqlite3.Connection) -> None:
            result["count"] = sweep_priorities_sql(conn, resolved, now=now)

        self._execute(action, "priority_sweep")
        return result["count"]

    # ------------------------------------------------------------------
    # Read-side helpers (fail-open, read-only)
    # ------------------------------------------------------------------

    def get_link_fetch_state(self, url: str) -> Optional[Dict[str, Any]]:
        """Read-only lookup of a link's gather status and conditional-revalidation token.

        Returns ``{"visit_status": str, "etag": str, "etag_transport": str}``
        when the link exists, or ``None`` when absent.  Uses a dedicated
        read-only connection so it never enqueues a write and never raises
        into a stage worker loop (design D6, mirrors ``FileMetaStore._conn``).
        """
        if not url or not self.enabled:
            return None
        try:
            h = url_hash(url)
            uri = f"file:{self.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            try:
                row = conn.execute(
                    "SELECT visit_status, etag, etag_transport FROM links WHERE url_hash = ?",
                    (h,),
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                return None
            return {
                "visit_status": row["visit_status"] or "",
                "etag": row["etag"] or "",
                "etag_transport": row["etag_transport"] or "",
            }
        except Exception:  # pragma: no cover - defensive
            return None

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
        repo_upserts = [payload for op, payload in batch if op == _OP_REPO_UPSERT]
        repo_touches = [payload for op, payload in batch if op == _OP_REPO_TOUCH]
        repo_gones = [payload for op, payload in batch if op == _OP_REPO_GONE]
        repo_links = [payload for op, payload in batch if op == _OP_REPO_LINKS]
        key_upserts = [payload for op, payload in batch if op == _OP_KEY_UPSERT]
        key_observes = [payload for op, payload in batch if op == _OP_KEY_OBSERVE]
        key_sources = [payload for op, payload in batch if op == _OP_KEY_SOURCE]
        file_upserts = [payload for op, payload in batch if op == _OP_FILE_UPSERT]
        file_touches = [payload for op, payload in batch if op == _OP_FILE_TOUCH]
        file_gones = [payload for op, payload in batch if op == _OP_FILE_GONE]
        file_links = [payload for op, payload in batch if op == _OP_FILE_LINKS]

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
            if repo_upserts:
                conn.executemany(_REPO_UPSERT_SQL, [self._repo_upsert_row(payload) for payload in repo_upserts])
            if repo_touches:
                conn.executemany(_REPO_TOUCH_SQL, [self._repo_touch_row(payload) for payload in repo_touches])
            if repo_gones:
                conn.executemany(_REPO_GONE_SQL, [self._repo_gone_row(payload) for payload in repo_gones])
            if repo_links:
                conn.executemany(_REPO_LINKS_SQL, [self._repo_links_row(payload) for payload in repo_links])
            if key_upserts:
                conn.executemany(_KEY_UPSERT_SQL, [self._key_upsert_row(payload) for payload in key_upserts])
            if key_observes:
                conn.executemany(_KEY_OBSERVE_SQL, [self._key_observe_row(payload) for payload in key_observes])
            if key_sources:
                conn.executemany(_KEY_SOURCE_SQL, [self._key_source_row(payload) for payload in key_sources])
            if file_upserts:
                conn.executemany(_FILE_UPSERT_SQL, [self._file_upsert_row(payload) for payload in file_upserts])
            if file_touches:
                conn.executemany(_FILE_TOUCH_SQL, [self._file_touch_row(payload) for payload in file_touches])
            if file_gones:
                conn.executemany(_FILE_GONE_SQL, [self._file_gone_row(payload) for payload in file_gones])
            if file_links:
                conn.executemany(_FILE_LINKS_SQL, [self._file_links_row(payload) for payload in file_links])

            # Priority denormalization (add-target-prioritization D3): evidence
            # changes mark their owning repositories dirty; the same transaction
            # rescores them so `links.priority` never lags the ledger by a batch.
            dirty_repos = set()
            if key_upserts:
                dirty_repos |= repos_for_url_hashes(
                    conn, (payload["source_url_hash"] for payload in key_upserts)
                )
            if key_sources:
                dirty_repos |= repos_for_url_hashes(
                    conn, (payload["url_hash"] for payload in key_sources)
                )
            if metadatas:
                dirty_repos |= repos_for_url_hashes(conn, (row[7] for row in rows))
            if repo_links:
                dirty_repos |= {
                    (payload["owner"], payload["repo"])
                    for payload in repo_links
                    if payload["owner"] and payload["repo"]
                }
            if dirty_repos:
                apply_scores(conn, score_many(conn, dirty_repos, self.priority_weights))

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
            payload.get("etag"),
            payload.get("etag_transport", ""),
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
        # Monotonic maximum (design D7): each date parameter is bound three
        # times for the CASE expression (NULL check, NULL-col check, MAX).
        return (
            repo_pushed_at, repo_pushed_at, repo_pushed_at,
            repo_size_kb,
            file_commit_date, file_commit_date, file_commit_date,
            digest,
        )

    @staticmethod
    def _repo_upsert_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            payload["owner"],
            payload["repo"],
            payload["pushed_at"],
            payload["size_kb"],
            payload["default_branch"],
            payload["etag"],
            payload["ts"],
            payload["gone"],
        )

    @staticmethod
    def _repo_touch_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (payload["ts"], payload["etag"], payload["owner"], payload["repo"])

    @staticmethod
    def _repo_gone_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (payload["owner"], payload["repo"], payload["ts"])

    @staticmethod
    def _repo_links_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        # Monotonic maximum (design D7): pushed_at is bound three times for the
        # CASE expression (NULL check, NULL-col check, MAX).
        return (
            payload["pushed_at"], payload["pushed_at"], payload["pushed_at"],
            payload["size_kb"],
            payload["owner"], payload["repo"],
        )

    @staticmethod
    def _key_upsert_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        ts = payload["ts"]
        return (
            payload["key_hash"],
            payload["provider"],
            payload["key_ref_masked"],
            payload["address"],
            payload["endpoint"],
            payload["status"],
            ts,
            ts,
            ts,
            payload["source_url_hash"],
        )

    @staticmethod
    def _key_observe_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (payload["ts"], payload["ts"], payload["key_hash"])

    @staticmethod
    def _key_source_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (payload["key_hash"], payload["url_hash"], payload["ts"], payload["ts"])

    @staticmethod
    def _file_upsert_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            payload["owner"],
            payload["repo"],
            payload["path"],
            payload["resolved_sha"],
            payload["committed_date"],
            payload["etag"],
            payload["ts"],
            payload["gone"],
        )

    @staticmethod
    def _file_touch_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (payload["ts"], payload["etag"], payload["owner"], payload["repo"], payload["path"])

    @staticmethod
    def _file_gone_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        return (payload["owner"], payload["repo"], payload["path"], payload["ts"])

    @staticmethod
    def _file_links_row(payload: Dict[str, Any]) -> Tuple[Any, ...]:
        # Monotonic maximum (design D7): committed_date bound three times.
        cd = payload["committed_date"]
        return (cd, cd, cd, payload["owner"], payload["repo"], payload["path"])

    def _enqueue(self, op: str, payload: Dict[str, Any]) -> None:
        if not self.enabled or not self._available or self._suppress_writes:
            return
        try:
            self._queue.put_nowait((op, payload))
        except queue.Full:
            with self._dropped_lock:
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


def init_registry(
    workspace: str,
    config: Any = None,
    enabled: Optional[bool] = None,
    prioritization: Any = None,
) -> Registry:
    """Create and install the process-wide registry instance."""
    global _registry
    _registry = Registry(workspace, config=config, enabled=enabled, prioritization=prioritization)
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
