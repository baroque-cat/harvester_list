#!/usr/bin/env python3

"""
Per-file freshness evidence cache (add-file-commit-dating D1/D2).

Stores per-file commit-date evidence keyed by ``(owner, repo, path)`` plus a
``resolved_sha`` for cache invalidation when the observed revision changes.
Writes are delegated to the single registry writer thread through
``record_file_*`` operations; reads use a read-only SQLite connection with
fail-open error counting.

TTL + ETag + gone suppression mirror the repository-level cache in
``storage/repo_meta.py``.  No network code in this module.
"""

import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from storage.registry import REGISTRY_FILENAME
from tools.logger import get_logger

logger = get_logger("storage")

TTL_HOURS_DEFAULT = 24.0
SECONDS_PER_HOUR = 3600.0
COMMITS_API_BASE = "https://api.github.com/repos"
GRAPHQL_API_URL = "https://api.github.com/graphql"


@dataclass(frozen=True)
class FileMetaEntry:
    """A cached ``file_meta`` row."""

    owner: str
    repo: str
    path: str
    resolved_sha: Optional[str] = None
    committed_date: Optional[float] = None
    etag: Optional[str] = None
    fetched_at: float = 0.0
    gone: int = 0

    @property
    def has_date(self) -> bool:
        """True when a committed_date is known."""
        return self.committed_date is not None

    def matches_sha(self, sha: Optional[str]) -> bool:
        """True when the entry's resolved_sha equals the given sha.

        Both empty/None counts as a match only when both are empty.
        """
        if not self.resolved_sha and not sha:
            return True
        return self.resolved_sha == sha


class FileMetaStore:
    """Read-side cache access plus write delegation to the registry writer.

    Mirrors :class:`storage.repo_meta.RepoMetaStore` in constructor shape,
    connection handling and fail-open discipline.
    """

    def __init__(
        self,
        workspace: str,
        ttl_hours: float = TTL_HOURS_DEFAULT,
        registry: Any = None,
        registry_path: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.workspace = str(workspace)
        self.ttl_hours = float(ttl_hours or TTL_HOURS_DEFAULT)
        self._registry = registry
        self.path = registry_path or os.path.join(self.workspace, REGISTRY_FILENAME)
        self.clock = clock
        self._local = threading.local()
        self.read_errors = 0

    # -- reads ---------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            uri = f"file:{self.path}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return conn

    def _query(self, sql: str, params: Sequence[Any] = ()) -> List[sqlite3.Row]:
        try:
            return self._conn().execute(sql, tuple(params)).fetchall()
        except Exception:  # missing table/db -> treat as no cache (fail-open)
            self.read_errors += 1
            return []

    @staticmethod
    def _row_to_entry(row: sqlite3.Row) -> FileMetaEntry:
        return FileMetaEntry(
            owner=row["owner"],
            repo=row["repo"],
            path=row["path"],
            resolved_sha=row["resolved_sha"],
            committed_date=row["committed_date"],
            etag=row["etag"],
            fetched_at=float(row["fetched_at"] or 0.0),
            gone=int(row["gone"] or 0),
        )

    def get(self, owner: str, repo: str, path: str) -> Optional[FileMetaEntry]:
        """Look up one cached file entry; None when absent or on error."""
        rows = self._query(
            "SELECT owner, repo, path, resolved_sha, committed_date, etag, fetched_at, gone "
            "FROM file_meta WHERE owner = ? AND repo = ? AND path = ?",
            (owner, repo, path),
        )
        return self._row_to_entry(rows[0]) if rows else None

    def get_many(
        self, triples: Iterable[Tuple[str, str, str]]
    ) -> Dict[Tuple[str, str, str], FileMetaEntry]:
        """Look up multiple entries; missing ones are omitted from the result."""
        result: Dict[Tuple[str, str, str], FileMetaEntry] = {}
        for owner, repo, path in self._dedup(triples):
            entry = self.get(owner, repo, path)
            if entry is not None:
                result[(owner, repo, path)] = entry
        return result

    def is_fresh(
        self,
        entry: Optional[FileMetaEntry],
        sha: Optional[str] = None,
        now: Optional[float] = None,
    ) -> bool:
        """A gone entry is also fresh within TTL: takedowns suppress retries.

        A ``sha`` argument that does not match ``entry.resolved_sha`` makes
        the entry stale (design D2: a different observed revision invalidates
        the cached entry).
        """
        if entry is None:
            return False
        if sha is not None and not entry.matches_sha(sha):
            return False
        moment = self.clock() if now is None else now
        return (moment - float(entry.fetched_at or 0.0)) < (self.ttl_hours * SECONDS_PER_HOUR)

    def stale_or_missing(
        self,
        triples_with_sha: Iterable[Tuple[str, str, str, Optional[str]]],
        now: Optional[float] = None,
    ) -> List[Tuple[str, str, str]]:
        """Resolve the exact resume query: entries absent or older than TTL.

        Each element is ``(owner, repo, path, sha)``; ``sha`` may be None.
        Returns the triples needing a fetch.
        """
        moment = self.clock() if now is None else now
        pending: List[Tuple[str, str, str]] = []
        for item in triples_with_sha or []:
            if len(item) == 4:
                owner, repo, path, sha = item
            else:
                owner, repo, path = item[:3]
                sha = None
            if not owner or not repo or not path:
                continue
            entry = self.get(owner, repo, path)
            if not self.is_fresh(entry, sha=sha, now=moment):
                pending.append((owner, repo, path))
        return pending

    # -- writes (delegated to the registry writer thread) --------------
    def record_upsert(
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
        """Replace the cached file metadata from a full response."""
        if self._registry is not None:
            self._registry.record_file_upsert(
                owner,
                repo,
                path,
                resolved_sha=resolved_sha,
                committed_date=committed_date,
                etag=etag,
                ts=ts,
                gone=gone,
            )

    def record_touch(
        self, owner: str, repo: str, path: str, ts: Optional[float] = None, etag: Optional[str] = None
    ) -> None:
        """Touch-only refresh after a 304 (fetched_at bump, values preserved)."""
        if self._registry is not None:
            self._registry.record_file_touch(owner, repo, path, ts=ts, etag=etag)

    def record_gone(self, owner: str, repo: str, path: str, ts: Optional[float] = None) -> None:
        """Record a not-found target without erasing previously known values."""
        if self._registry is not None:
            self._registry.record_file_gone(owner, repo, path, ts=ts)

    def record_link_date(
        self, owner: str, repo: str, path: str, committed_date: Optional[float] = None
    ) -> None:
        """Propagate a known committed_date to links rows (UPDATE-only)."""
        if self._registry is not None:
            self._registry.record_file_link_metadata(owner, repo, path, committed_date=committed_date)

    def flush(self, timeout: float = 10.0) -> None:
        if self._registry is not None:
            try:
                self._registry.flush(timeout)
            except Exception:  # pragma: no cover - defensive
                pass

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - defensive
                pass
            self._local.conn = None

    @staticmethod
    def _dedup(triples: Iterable[Tuple[str, str, str]]) -> List[Tuple[str, str, str]]:
        seen = set()
        ordered: List[Tuple[str, str, str]] = []
        for item in triples or []:
            if len(item) >= 3:
                owner, repo, path = item[0], item[1], item[2]
            else:
                continue
            if not owner or not repo or not path:
                continue
            key = (owner, repo, path)
            if key not in seen:
                seen.add(key)
                ordered.append(key)
        return ordered
