#!/usr/bin/env python3

"""
Per-file commit-date dating orchestrator (add-file-commit-dating D8/D9/D12).

Mirrors :class:`storage.repo_meta.RepoMetaEnricher` in construction, credential
rotation, cooling-down discipline and fail-open posture.  Inert when
``enabled`` is False: zero requests, zero writes, ``to_stats()["enabled"]``
is False (spec S11/S20).

The orchestrator does NOT talk to the network directly; it delegates to
:func:`search.client.fetch_file_commit_date` (REST) and
:func:`search.client.fetch_file_commit_dates_batch` (GraphQL), which already
carry the typed-signal classification (design D5).
"""

import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from search.client import (
    FileDateResult,
    fetch_file_commit_date,
    fetch_file_commit_dates_batch,
    get_file_date_stats,
    _parse_blob_link,
)
from storage.file_meta import FileMetaStore
from tools.logger import get_logger
from tools.state import CredentialsExhausted, GithubCredentialLimited

logger = get_logger("storage")

# Wall-clock budget for one date_urls/date_urls_map call (F3, gate arm C).
# At 0.2 s⁻¹ a GraphQL batch costs 5 s of wall clock; 5 s bounds the single
# search thread so it cannot starve gather for an entire run.  Mirrors
# ``RepoMetaEnricher.DEFAULT_BATCH_BUDGET_SECONDS = 60.0`` in spirit but is
# tighter because dating rides the search thread, not its own enricher thread.
DEFAULT_DATING_BUDGET_SECONDS = 5.0


class FileDater:
    """Cache-first, budgeted, fail-open per-file commit-date fetcher.

    Constructor mirrors ``RepoMetaEnricher``: the ``store`` is a
    :class:`FileMetaStore`, ``auth`` is the credential provider, ``client``
    is the shared HTTP opener, and ``cooling_probe`` is the non-blocking
    all-tokens-cooling check.

    Design D9: inert when ``enabled`` is False.  ``date_urls`` returns 0,
    ``to_stats()["enabled"]`` is False, and no counter is touched.
    """

    def __init__(
        self,
        store: FileMetaStore,
        auth: Any = None,
        client: Any = None,
        enabled: bool = False,
        transport: str = "rest",
        scope: str = "keyed",
        max_requests_per_run: int = 200,
        graphql_batch_size: int = 4,
        cooling_probe: Optional[Callable[[], bool]] = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 30.0,
        retries: int = 3,
        flush_timeout: float = 10.0,
        max_credential_retries: int = 3,
    ):
        self.store = store
        self.enabled = bool(enabled)
        self.transport = str(transport or "rest").strip().lower()
        self.scope = str(scope or "keyed").strip().lower()
        self.max_requests_per_run = max(0, int(max_requests_per_run))
        self.graphql_batch_size = max(1, int(graphql_batch_size))
        self._auth = auth
        self._client = client
        self._cooling_probe = cooling_probe
        self._clock = clock
        self.timeout = float(timeout)
        self.retries = max(1, int(retries))
        self.flush_timeout = float(flush_timeout)
        self.max_credential_retries = max(1, int(max_credential_retries))

        self._lock = threading.Lock()
        self._counters_lock = threading.Lock()
        self._cached_hits = 0
        self._undateable_links = 0
        self._requests_this_run = 0
        self._budget_stopped = 0
        self._time_stopped = 0

    # -- public API ----------------------------------------------------
    def date_urls_map(
        self,
        urls: Iterable[str],
        flush: bool = True,
        budget: Optional[float] = None,
    ) -> Dict[str, Optional[float]]:
        """Parse each URL, consult the cache, fetch stale-or-missing entries.

        Returns ``{input_url: committed_date_or_None}`` — the date is the
        committed_date from the cache or from a fresh fetch, or ``None`` when
        the file has no known date (404, absent body, unparseable URL, budget
        truncation).  The caller merges the result into the URL-keyed metadata
        channel (``output.add_link_metadata`` → ``Registry.record_metadata`` →
        ``_METADATA_SQL`` keyed by ``url_hash``); this is the correct
        propagation path because ``links.path`` stores the ``blob/<ref>/…``
        prefix while ``file_meta.path`` stores the clean path, so the
        ``(owner, repo, path)``-keyed ``_FILE_LINKS_SQL`` never matches
        (gate arm B: ``fill_file_commit_date = 0`` despite 73 dated rows).

        ``budget`` bounds wall-clock seconds for this call (F3, gate arm C).
        Checked between files/batches; on truncation the remainder is left for
        a later trigger — the cache is the ledger, nothing is lost.
        """
        result_map: Dict[str, Optional[float]] = {}
        if not self.enabled:
            for url in urls or []:
                result_map[url] = None
            return result_map

        deadline = None
        if budget is not None:
            deadline = self._clock() + float(budget)

        # Parse URLs into (url, owner, repo, path, ref) tuples, preserving the
        # original URL so the caller can merge by key.
        items: List[Tuple[str, str, str, str, str]] = []
        seen: set = set()
        for url in urls or []:
            parsed = _parse_blob_link(url)
            if parsed is None:
                with self._counters_lock:
                    self._undateable_links += 1
                result_map[url] = None
                continue
            key = (parsed["owner"], parsed["repo"], parsed["path"])
            if key in seen:
                # Duplicate URL (same identity): reuse the first URL's result.
                result_map[url] = None  # filled below after fetch
                continue
            seen.add(key)
            items.append((url, parsed["owner"], parsed["repo"], parsed["path"], parsed["ref"]))

        if not items:
            return result_map

        # Phase 1: consult the cache.  Fresh entries propagate without a request.
        stale: List[Tuple[str, str, str, str, str]] = []
        for url, owner, repo, path, ref in items:
            entry = self.store.get(owner, repo, path)
            if self.store.is_fresh(entry, sha=ref):
                # NOTE: record_link_date is kept for cache-side bookkeeping but
                # the actual link propagation goes through the URL-keyed channel
                # (output.add_link_metadata → record_metadata → _METADATA_SQL)
                # because links.path stores blob/<ref>/… while file_meta.path
                # stores the clean path — the (owner,repo,path) match in
                # _FILE_LINKS_SQL never succeeds (gate arm B finding).
                if entry is not None and entry.has_date:
                    self.store.record_link_date(owner, repo, path, committed_date=entry.committed_date)
                    result_map[url] = entry.committed_date
                else:
                    result_map[url] = None
                with self._counters_lock:
                    self._cached_hits += 1
            else:
                stale.append((url, owner, repo, path, ref))
                result_map[url] = None  # filled below after fetch

        if not stale:
            return result_map

        # Phase 2: fetch stale-or-missing entries, honouring the per-run budget
        # and the wall-clock budget.
        if self.transport == "graphql":
            self._fetch_graphql(stale, flush, deadline=deadline, result_map=result_map)
        else:
            self._fetch_rest(stale, flush, deadline=deadline, result_map=result_map)

        return result_map

    def date_urls(self, urls: Iterable[str], flush: bool = True) -> int:
        """Parse each URL, consult the cache, fetch stale-or-missing entries.

        Returns the number of files successfully dated (200 with a date, or
        304 cached).  Unparseable URLs are counted as ``undateable_link``
        and skipped.  ``GithubCredentialLimited`` propagates exactly as
        ``RepoMetaEnricher`` does for credential rotation.  Every other
        failure is fail-open.

        Delegates to :meth:`date_urls_map` and counts non-None values.
        """
        url_list = list(urls or [])
        date_map = self.date_urls_map(url_list, flush=flush)
        return sum(1 for v in date_map.values() if v is not None)

    def to_stats(self) -> Dict[str, Any]:
        """Flatten counters for ``PipelineStatus.file_dating_metrics`` (design D12).

        Additive only: no counter is renamed or removed.  When disabled,
        returns ``{"enabled": False}`` so the renderer can suppress the line.
        """
        if not self.enabled:
            return {"enabled": False}
        file_stats = get_file_date_stats()
        with self._counters_lock:
            filled = int(file_stats.get("file_dating_filled", 0))
            absent = int(file_stats.get("file_dating_absent", 0))
            total_attempted = filled + absent
            fill_rate = filled / total_attempted if total_attempted > 0 else 0.0
            return {
                "enabled": True,
                "scope": self.scope,
                "transport": self.transport,
                # F4: use the authoritative transport-independent counter.
                # The client-side file_dating_requests stat is incremented by
                # fetch_file_commit_date (REST) and now also by
                # fetch_file_commit_dates_batch (GraphQL, once per POST), so
                # both transports agree.  _requests_this_run is the dater's
                # own count, which is the authoritative figure.
                "file_dating_requests": self._requests_this_run,
                "file_dating_304s": int(file_stats.get("file_dating_304s", 0)),
                "file_dating_filled": filled,
                "file_dating_absent": absent,
                "file_dating_batches": int(file_stats.get("file_dating_batches", 0)),
                "file_dating_graphql_cost": int(file_stats.get("file_dating_graphql_cost", 0)),
                "file_dating_refusals": int(file_stats.get("file_dating_refusals", 0)),
                "file_dating_budget_stopped": self._budget_stopped,
                "file_dating_time_stopped": self._time_stopped,
                "cached_hits": self._cached_hits,
                "undateable_link": self._undateable_links,
                "requests_this_run": self._requests_this_run,
                "budget_remaining": max(0, self.max_requests_per_run - self._requests_this_run),
                "fill_rate": fill_rate,
            }

    def close(self) -> None:
        """Flush and close the underlying store (mirrors ``RepoMetaEnricher.close``)."""
        try:
            self.store.flush(self.flush_timeout)
        except Exception:  # pragma: no cover - defensive
            pass
        self.store.close()

    # -- internals -----------------------------------------------------
    def _budget_exhausted(self) -> bool:
        """True when the per-run request ceiling has been reached."""
        return self._requests_this_run >= self.max_requests_per_run

    def _time_exhausted(self, deadline: Optional[float]) -> bool:
        """True when the wall-clock budget has been spent (F3)."""
        if deadline is None:
            return False
        return self._clock() >= deadline

    def _next_credential(self) -> Optional[str]:
        """Acquire the next credential, or None when the pool is exhausted/cooling."""
        if self._auth is None:
            return None
        try:
            return self._auth.get_token()
        except CredentialsExhausted:
            return None
        except Exception:  # pragma: no cover - defensive
            return None

    def _fetch_rest(
        self,
        stale: List[Tuple[str, str, str, str, str]],
        flush: bool,
        deadline: Optional[float] = None,
        result_map: Optional[Dict[str, Optional[float]]] = None,
    ) -> int:
        """One REST request per file; honour the per-run ceiling (design D9)."""
        dated = 0
        for url, owner, repo, path, ref in stale:
            if self._budget_exhausted():
                with self._counters_lock:
                    self._budget_stopped += 1
                break

            # F3: wall-clock budget check between files.
            if self._time_exhausted(deadline):
                with self._counters_lock:
                    self._time_stopped += 1
                break

            # Cooling probe: yield silently when all tokens are cooling.
            if self._cooling_probe is not None:
                try:
                    if bool(self._cooling_probe()):
                        continue
                except Exception:  # pragma: no cover - defensive
                    pass

            for _ in range(self.max_credential_retries):
                credential = self._next_credential()
                if credential is None:
                    break
                try:
                    result = fetch_file_commit_date(
                        owner, repo, path,
                        sha=ref,
                        credential=credential,
                        client=self._client,
                        timeout=self.timeout,
                        retries=self.retries,
                    )
                except GithubCredentialLimited:
                    continue  # rotate to the next credential
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(f"[file_dating] REST fetch failed (fail-open): {exc}")
                    break

                with self._counters_lock:
                    self._requests_this_run += 1
                self._apply_result(owner, repo, path, ref, result, flush)
                if result.status == 200 and result.committed_date is not None:
                    dated += 1
                    if result_map is not None:
                        result_map[url] = result.committed_date
                break
        return dated

    def _fetch_graphql(
        self,
        stale: List[Tuple[str, str, str, str, str]],
        flush: bool,
        deadline: Optional[float] = None,
        result_map: Optional[Dict[str, Optional[float]]] = None,
    ) -> int:
        """Batch GraphQL requests; honour the per-run ceiling (design D9)."""
        dated = 0
        batch_size = self.graphql_batch_size
        for batch_start in range(0, len(stale), batch_size):
            if self._budget_exhausted():
                with self._counters_lock:
                    self._budget_stopped += len(stale) - batch_start
                break

            # F3: wall-clock budget check between batches.
            if self._time_exhausted(deadline):
                with self._counters_lock:
                    self._time_stopped += 1
                break

            # Cooling probe
            if self._cooling_probe is not None:
                try:
                    if bool(self._cooling_probe()):
                        continue
                except Exception:  # pragma: no cover - defensive
                    pass

            batch = stale[batch_start:batch_start + batch_size]
            # Only batch items with a usable hex40 sha.
            # Batch entries are (url, owner, repo, path, ref).
            items = [(o, r, p, ref) for _, o, r, p, ref in batch if ref and len(ref) == 40]
            batch_urls = [url for url, o, r, p, ref in batch if ref and len(ref) == 40]
            if not items:
                # Items without usable shas: fall back to REST individually.
                for url, owner, repo, path, ref in batch:
                    if self._budget_exhausted():
                        with self._counters_lock:
                            self._budget_stopped += 1
                        break
                    if self._time_exhausted(deadline):
                        with self._counters_lock:
                            self._time_stopped += 1
                        break
                    for _ in range(self.max_credential_retries):
                        credential = self._next_credential()
                        if credential is None:
                            break
                        try:
                            result = fetch_file_commit_date(
                                owner, repo, path,
                                sha=ref,
                                credential=credential,
                                client=self._client,
                                timeout=self.timeout,
                                retries=self.retries,
                            )
                        except GithubCredentialLimited:
                            continue
                        except Exception:
                            break
                        with self._counters_lock:
                            self._requests_this_run += 1
                        self._apply_result(owner, repo, path, ref, result, flush)
                        if result.status == 200 and result.committed_date is not None:
                            dated += 1
                            if result_map is not None:
                                result_map[url] = result.committed_date
                        break
                continue

            for _ in range(self.max_credential_retries):
                credential = self._next_credential()
                if credential is None:
                    break
                try:
                    results = fetch_file_commit_dates_batch(
                        items,
                        credential=credential,
                        client=self._client,
                        timeout=self.timeout,
                        retries=self.retries,
                    )
                except GithubCredentialLimited:
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    logger.debug(f"[file_dating] GraphQL batch failed (fail-open): {exc}")
                    break

                with self._counters_lock:
                    self._requests_this_run += 1
                for idx, ((owner, repo, path), result) in enumerate(results.items()):
                    ref = ""
                    for o, r, p, r_ref in items:
                        if (o, r, p) == (owner, repo, path):
                            ref = r_ref
                            break
                    self._apply_result(owner, repo, path, ref, result, flush)
                    if result.status == 200 and result.committed_date is not None:
                        dated += 1
                        if result_map is not None and idx < len(batch_urls):
                            result_map[batch_urls[idx]] = result.committed_date
                break
        return dated

    def _apply_result(
        self, owner: str, repo: str, path: str, ref: str,
        result: FileDateResult, flush: bool,
    ) -> None:
        """Write the fetch outcome to the cache and propagate to links."""
        now = self._clock()
        if result.status == 200:
            self.store.record_upsert(
                owner, repo, path,
                resolved_sha=result.sha or ref,
                committed_date=result.committed_date,
                etag=result.etag,
                ts=now,
                gone=0,
            )
            # NOTE: record_link_date is kept for cache-side bookkeeping but
            # the actual link propagation goes through the URL-keyed channel
            # (output.add_link_metadata → record_metadata → _METADATA_SQL)
            # because links.path stores blob/<ref>/… while file_meta.path
            # stores the clean path — the (owner,repo,path) match in
            # _FILE_LINKS_SQL never succeeds (gate arm B finding).
            if result.committed_date is not None:
                self.store.record_link_date(owner, repo, path, committed_date=result.committed_date)
            if flush:
                self.store.flush(self.flush_timeout)
        elif result.status == 304:
            self.store.record_touch(owner, repo, path, ts=now, etag=result.etag)
            # Re-propagate cached date (design D7: monotonic, never regresses).
            entry = self.store.get(owner, repo, path)
            if entry is not None and entry.has_date:
                self.store.record_link_date(owner, repo, path, committed_date=entry.committed_date)
            if flush:
                self.store.flush(self.flush_timeout)
        elif result.status == 404:
            self.store.record_gone(owner, repo, path, ts=now)
            if flush:
                self.store.flush(self.flush_timeout)
