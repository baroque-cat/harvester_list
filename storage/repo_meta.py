#!/usr/bin/env python3

"""
TTL-cached repository metadata enrichment (add-repo-meta-enrichment).

Restores repository-level freshness evidence (``pushed_at``, ``size_kb``,
``default_branch``) through an authenticated ``GET /repos/{owner}/{repo}``
channel that is TTL-cached and ETag-conditioned:

* the ``repos`` table is the cache AND the work ledger (design D7), written
  through the single registry writer thread so a crash resumes only the
  stale-or-missing set;
* ``if-none-match`` turns an unchanged refresh into a near-free ``304`` (design
  D3);
* values propagate into ``links.repo_pushed_at``/``repo_size_kb`` through an
  UPDATE-only COALESCE merge (never fabricates link rows);
* failures are always fail-open, and a deployment with no usable API token
  silently self-disables.
"""

import json
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from constant.system import GITHUB_API_TIMEOUT, SERVICE_TYPE_GITHUB_API
from storage.registry import REGISTRY_FILENAME, parse_github_url
from tools.logger import get_logger
from tools.state import CredentialsExhausted, GithubCredentialLimited, github_credential_state

logger = get_logger("storage")

TTL_HOURS_DEFAULT = 24.0
SECONDS_PER_HOUR = 3600.0
REPO_API_ACCEPT = "application/vnd.github+json"
REPO_API_VERSION = "2022-11-28"
REPO_API_BASE = "https://api.github.com/repos"
# Wall-clock budget for one lazily-triggered enrichment batch (design D4:
# "synchronously-with-timeout, bounded, fail-open").  Checked between repos;
# an in-flight fetch always completes.  Truncated work is not lost: the cache
# is the ledger, so later triggers pick up the stale-or-missing remainder.
DEFAULT_BATCH_BUDGET_SECONDS = 60.0


def tokens_cooling_down() -> bool:
    """Non-blocking probe: True when API tokens exist but ALL are cooling down.

    ``Credentials.get_token()`` sleeps while every token cools (search's own
    blocking policy).  Enrichment must never queue behind that sleep (spec R4:
    silently yield, zero requests), so the pipeline wires this probe instead.
    An empty pool returns False on purpose -- that permanent case is handled by
    the credential path itself (tokenless latch).  Fails open to False.
    """
    try:
        from tools.coordinator import get_credentials_manager

        credentials = get_credentials_manager()
        if not credentials.has_tokens():
            return False
        return github_credential_state.all_cooling(SERVICE_TYPE_GITHUB_API, credentials.tokens)
    except Exception:  # pragma: no cover - managers not initialized / embedded use
        return False


@dataclass(frozen=True)
class RepoMetaEntry:
    """A cached ``repos`` row."""

    owner: str
    repo: str
    pushed_at: Optional[float] = None
    size_kb: Optional[int] = None
    default_branch: Optional[str] = None
    etag: Optional[str] = None
    fetched_at: float = 0.0
    gone: int = 0

    @property
    def has_pushed_at(self) -> bool:
        return self.pushed_at is not None


@dataclass
class RepoFetchResult:
    """Outcome of one ``GET /repos/{owner}/{repo}`` exchange."""

    owner: str
    repo: str
    status: int
    pushed_at: Optional[float] = None
    size_kb: Optional[int] = None
    default_branch: Optional[str] = None
    etag: Optional[str] = None
    error: str = ""


def _header(headers: Optional[Dict[str, Any]], name: str) -> str:
    """Case-insensitive header lookup."""
    target = name.lower()
    for key, value in (headers or {}).items():
        if str(key).lower() == target and value is not None:
            return str(value)
    return ""


def _coerce_size_kb(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _load_json(content: Any) -> Any:
    if isinstance(content, (dict, list)):
        return content
    if not content:
        return None
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


def fetch_repo_metadata(
    owner: str,
    repo: str,
    etag: Optional[str] = None,
    credential: Optional[str] = None,
    client: Any = None,
    timeout: float = GITHUB_API_TIMEOUT,
    retries: int = 3,
) -> RepoFetchResult:
    """Fetch one repository's metadata over the shared ``github_api`` client.

    ``GithubCredentialLimited`` is deliberately allowed to propagate so the
    caller can rotate credentials exactly as the search stages do.  Every other
    failure degrades to ``status=0`` (fail-open).
    """
    from search import client as search_client

    if client is None:
        client = search_client.get_github_client()

    url = f"{REPO_API_BASE}/{owner}/{repo}"
    headers: Dict[str, str] = {
        "Accept": REPO_API_ACCEPT,
        "X-GitHub-Api-Version": REPO_API_VERSION,
    }
    if credential:
        headers["Authorization"] = f"Bearer {credential}"
    if etag:
        headers["If-None-Match"] = etag

    try:
        status, content, response_headers = client.get_with_status(
            url,
            headers=headers,
            params=None,
            retries=retries,
            interval=0,
            timeout=timeout,
            credential=credential,
        )
    except GithubCredentialLimited:
        raise
    except Exception as exc:  # network/transport failure -> fail-open
        return RepoFetchResult(owner, repo, 0, error=str(exc))

    status = int(status or 0)
    response_etag = _header(response_headers, "etag")

    if status == 304:
        return RepoFetchResult(owner, repo, 304, etag=response_etag or etag)
    if status == 404:
        return RepoFetchResult(owner, repo, 404)
    if status == 200:
        body = _load_json(content)
        if not isinstance(body, dict):
            return RepoFetchResult(owner, repo, 200, etag=response_etag)
        from search.client import parse_iso_epoch

        return RepoFetchResult(
            owner,
            repo,
            200,
            pushed_at=parse_iso_epoch(body.get("pushed_at")),
            size_kb=_coerce_size_kb(body.get("size")),
            default_branch=body.get("default_branch"),
            etag=response_etag,
        )
    return RepoFetchResult(owner, repo, status, error=f"unexpected status {status}")


class RepoMetaStore:
    """Read-side cache access plus write delegation to the registry writer."""

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
    def _row_to_entry(row: sqlite3.Row) -> RepoMetaEntry:
        return RepoMetaEntry(
            owner=row["owner"],
            repo=row["repo"],
            pushed_at=row["pushed_at"],
            size_kb=row["size_kb"],
            default_branch=row["default_branch"],
            etag=row["etag"],
            fetched_at=float(row["fetched_at"] or 0.0),
            gone=int(row["gone"] or 0),
        )

    def get(self, owner: str, repo: str) -> Optional[RepoMetaEntry]:
        rows = self._query(
            "SELECT owner, repo, pushed_at, size_kb, default_branch, etag, fetched_at, gone "
            "FROM repos WHERE owner = ? AND repo = ?",
            (owner, repo),
        )
        return self._row_to_entry(rows[0]) if rows else None

    def get_many(self, pairs: Iterable[Tuple[str, str]]) -> Dict[Tuple[str, str], RepoMetaEntry]:
        result: Dict[Tuple[str, str], RepoMetaEntry] = {}
        for owner, repo in self._dedup(pairs):
            entry = self.get(owner, repo)
            if entry is not None:
                result[(owner, repo)] = entry
        return result

    def is_fresh(self, entry: Optional[RepoMetaEntry], now: Optional[float] = None) -> bool:
        """A gone entry is also fresh within TTL: takedowns suppress retries."""
        if entry is None:
            return False
        moment = self.clock() if now is None else now
        return (moment - float(entry.fetched_at or 0.0)) < (self.ttl_hours * SECONDS_PER_HOUR)

    def stale_or_missing(
        self, pairs: Iterable[Tuple[str, str]], now: Optional[float] = None
    ) -> List[Tuple[str, str]]:
        """Resolve the exact resume query: entries absent or older than TTL."""
        moment = self.clock() if now is None else now
        pending: List[Tuple[str, str]] = []
        for owner, repo in self._dedup(pairs):
            entry = self.get(owner, repo)
            if not self.is_fresh(entry, moment):
                pending.append((owner, repo))
        return pending

    # -- writes (delegated to the registry writer thread) --------------
    def record_upsert(
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
        if self._registry is not None:
            self._registry.record_repo_upsert(
                owner,
                repo,
                pushed_at=pushed_at,
                size_kb=size_kb,
                default_branch=default_branch,
                etag=etag,
                ts=ts,
                gone=gone,
            )

    def record_touch(self, owner: str, repo: str, ts: Optional[float] = None, etag: Optional[str] = None) -> None:
        if self._registry is not None:
            self._registry.record_repo_touch(owner, repo, ts=ts, etag=etag)

    def record_gone(self, owner: str, repo: str, ts: Optional[float] = None) -> None:
        if self._registry is not None:
            self._registry.record_repo_gone(owner, repo, ts=ts)

    def record_link_metadata(
        self, owner: str, repo: str, pushed_at: Optional[float] = None, size_kb: Optional[int] = None
    ) -> None:
        if self._registry is not None:
            self._registry.record_repo_link_metadata(owner, repo, pushed_at=pushed_at, size_kb=size_kb)

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
    def _dedup(pairs: Iterable[Tuple[str, str]]) -> List[Tuple[str, str]]:
        seen = set()
        ordered: List[Tuple[str, str]] = []
        for owner, repo in pairs or []:
            if not owner or not repo:
                continue
            key = (owner, repo)
            if key not in seen:
                seen.add(key)
                ordered.append(key)
        return ordered


class RepoMetaEnricher:
    """Cache-first, deduplicating, fail-open repository metadata fetcher."""

    def __init__(
        self,
        store: RepoMetaStore,
        auth: Any = None,
        client: Any = None,
        enabled: bool = False,
        ttl_hours: float = TTL_HOURS_DEFAULT,
        clock: Callable[[], float] = time.time,
        timeout: float = GITHUB_API_TIMEOUT,
        retries: int = 3,
        flush_timeout: float = 10.0,
        max_credential_retries: int = 3,
        credential_provider: Optional[Callable[[], Optional[str]]] = None,
        cooling_probe: Optional[Callable[[], bool]] = None,
        batch_budget: float = DEFAULT_BATCH_BUDGET_SECONDS,
    ):
        self.store = store
        self.enabled = bool(enabled)
        self.ttl_hours = float(ttl_hours or TTL_HOURS_DEFAULT)
        self._auth = auth
        self._client = client
        self._credential_provider = credential_provider
        self._cooling_probe = cooling_probe
        self.batch_budget = float(batch_budget)
        self._clock = clock
        self.timeout = timeout
        self.retries = retries
        self.flush_timeout = flush_timeout
        self.max_credential_retries = max(1, int(max_credential_retries))

        self._inflight: set = set()
        self._lock = threading.Lock()
        self._counters_lock = threading.Lock()
        self._fetches = 0
        self._threes = 0
        self._failures = 0
        self._warnings = 0
        self._gone = 0
        self._repos_cached = 0
        self._tokenless = False
        self._tokenless_logs = 0
        self._cooling_skips = 0
        self._cooling_logged = False

    # -- public API ----------------------------------------------------
    def enrich_urls(self, urls: Iterable[str], flush: bool = True, budget: Optional[float] = None) -> int:
        """Resolve distinct GitHub repositories from URLs and enrich them."""
        pairs: List[Tuple[str, str]] = []
        seen = set()
        for url in urls or []:
            owner, repo, _ = parse_github_url(url)
            if owner and repo and (owner, repo) not in seen:
                seen.add((owner, repo))
                pairs.append((owner, repo))
        return self.enrich_pairs(pairs, flush=flush, budget=budget)

    def enrich_pairs(self, pairs: Iterable[Tuple[str, str]], flush: bool = True, budget: Optional[float] = None) -> int:
        """Fetch every stale-or-missing ``(owner, repo)`` at most once.

        ``budget`` bounds the wall-clock time spent starting new fetches in
        this batch (default ``self.batch_budget``; ``math.inf`` disables).
        The check runs between repositories, so an in-flight fetch always
        completes; the remainder stays stale-or-missing in the ledger and is
        picked up by a later trigger (fail-open, design D4/D7).
        """
        if not self.enabled:
            return 0
        ordered = self.store._dedup(pairs)
        if not ordered:
            return 0

        limit = self.batch_budget if budget is None else float(budget)
        deadline = None if math.isinf(limit) else time.monotonic() + max(0.0, limit)

        fetched = 0
        for owner, repo in ordered:
            if deadline is not None and time.monotonic() >= deadline:
                logger.debug("[enrichment] batch budget exhausted; deferring remaining repositories")
                break
            if not self._claim(owner, repo):
                continue
            try:
                now = self._clock()
                entry = self.store.get(owner, repo)
                if self.store.is_fresh(entry, now):
                    # Late-discovered links of an already-cached repository must
                    # still receive its metadata; serve them from the cache with
                    # zero HTTP requests (same merge channel as the 200 path).
                    self._propagate_cached(owner, repo, entry)
                    continue
                if self._enrich_one(owner, repo, entry, flush):
                    fetched += 1
                if self._tokenless:
                    break
            finally:
                self._release(owner, repo)
        return fetched

    def to_stats(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"enabled": False}
        with self._counters_lock:
            return {
                "enabled": True,
                "enrichment_fetches": self._fetches,
                "enrichment_304s": self._threes,
                "enrichment_failures": self._failures,
                "enrichment_warnings": self._warnings,
                "repos_cached": self._repos_cached,
                "gone": self._gone,
                "cooling_skips": self._cooling_skips,
                "disabled_tokenless": self._tokenless,
                "tokenless_logs": self._tokenless_logs,
            }

    # -- internals -----------------------------------------------------
    def _claim(self, owner: str, repo: str) -> bool:
        with self._lock:
            key = (owner, repo)
            if key in self._inflight:
                return False
            self._inflight.add(key)
            return True

    def _release(self, owner: str, repo: str) -> None:
        with self._lock:
            self._inflight.discard((owner, repo))

    def _next_credential(self) -> Optional[str]:
        if self._credential_provider is not None:
            try:
                return self._credential_provider()
            except CredentialsExhausted:
                # Exhaustion is transient: yield silently like all-cooling and
                # never trip the permanent tokenless latch (design D8).
                self._note_cooling()
                return None
            except Exception:  # pragma: no cover - defensive
                return None
        if self._auth is None:
            return None
        try:
            return self._auth.get_token()
        except CredentialsExhausted:
            self._note_cooling()
            return None
        except Exception:  # pragma: no cover - defensive
            return None

    def _enrich_one(self, owner: str, repo: str, entry: Optional[RepoMetaEntry], flush: bool) -> bool:
        # Spec R4: when every API token is cooling down, yield silently instead
        # of blocking behind Credentials.get_token()'s cooldown sleep.  This is
        # transient (no latch): a later encounter retries once tokens recover.
        if self._cooling_probe is not None:
            try:
                cooling = bool(self._cooling_probe())
            except Exception:  # pragma: no cover - defensive
                cooling = False
            if cooling:
                self._note_cooling()
                return False

        etag = entry.etag if entry is not None else None
        for _ in range(self.max_credential_retries):
            credential = self._next_credential()
            if credential is None:
                self._disable_tokenless()
                return False
            try:
                result = fetch_repo_metadata(
                    owner,
                    repo,
                    etag=etag,
                    credential=credential,
                    client=self._client,
                    timeout=self.timeout,
                    retries=self.retries,
                )
            except GithubCredentialLimited:
                continue  # rotate to the next credential, exactly like search
            except Exception as exc:  # pragma: no cover - defensive
                self._record_failure(exc)
                return False

            if result.status in (200, 304, 404):
                self._apply_result(owner, repo, result, entry)
                if flush:
                    self.store.flush(self.flush_timeout)
                return True

            self._record_failure(RuntimeError(result.error or f"status {result.status}"))
            return False

        self._record_failure(RuntimeError("github credentials exhausted"))
        return False

    def _apply_result(
        self, owner: str, repo: str, result: RepoFetchResult, entry: Optional[RepoMetaEntry] = None
    ) -> None:
        now = self._clock()
        if result.status == 200:
            self.store.record_upsert(
                owner,
                repo,
                pushed_at=result.pushed_at,
                size_kb=result.size_kb,
                default_branch=result.default_branch,
                etag=result.etag,
                ts=now,
                gone=0,
            )
            self.store.record_link_metadata(owner, repo, pushed_at=result.pushed_at, size_kb=result.size_kb)
            with self._counters_lock:
                self._fetches += 1
                self._repos_cached += 1
        elif result.status == 304:
            self.store.record_touch(owner, repo, ts=now, etag=result.etag)
            # A 304 preserves the stored values; re-propagate them so links
            # discovered after the caching event are not left NULL until a 200
            # that may never come for static repositories.  Zero extra HTTP.
            self._propagate_cached(owner, repo, entry)
            with self._counters_lock:
                self._fetches += 1
                self._threes += 1
        elif result.status == 404:
            self.store.record_gone(owner, repo, ts=now)
            with self._counters_lock:
                self._fetches += 1
                self._gone += 1

    def _propagate_cached(self, owner: str, repo: str, entry: Optional[RepoMetaEntry]) -> None:
        """Merge cached repository metadata into its links without any HTTP.

        Covers the late-link gap (observed live 2026-09-19): links discovered
        AFTER a repository was cached would otherwise keep NULL
        ``repo_pushed_at``/``repo_size_kb`` until the next real 200 -- which
        may never come for static repositories, because the fresh-skip path
        issues no request and a 304 refresh previously merged nothing.  The
        merge is UPDATE-only COALESCE, so re-propagating known values is
        idempotent and never fabricates link rows.
        """
        if entry is None:
            return
        if entry.pushed_at is None and entry.size_kb is None:
            return
        self.store.record_link_metadata(owner, repo, pushed_at=entry.pushed_at, size_kb=entry.size_kb)

    def _record_failure(self, exc: Exception) -> None:
        with self._counters_lock:
            self._failures += 1
            first = self._warnings == 0
            if first:
                self._warnings += 1
        if first:
            logger.warning(f"[enrichment] repository metadata fetch failed; continuing fail-open: {exc}")

    def _note_cooling(self) -> None:
        """Count a transient all-tokens-cooling yield; log at most once."""
        with self._counters_lock:
            self._cooling_skips += 1
            first = not self._cooling_logged
            if first:
                self._cooling_logged = True
        if first:
            logger.info("[enrichment] all GitHub API tokens cooling down; yielding to search (zero requests)")

    def _disable_tokenless(self) -> None:
        with self._counters_lock:
            if self._tokenless:
                return
            self._tokenless = True
            self._tokenless_logs += 1
        logger.info("[enrichment] no usable GitHub API token; repository metadata enrichment disabled")

    def close(self) -> None:
        self.store.close()
