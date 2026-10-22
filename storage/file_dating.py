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

# Opportunistic coalescing window (P21).  Small gather calls (one URL each)
# wait up to this long for siblings to arrive so they can share a single
# GraphQL POST.  The window is paid only by small calls; it is cheap next to
# the github_graphql basket (base_rate 0.5 ⇒ one token per 2 s per credential),
# which today serialises four independent POSTs from four gather threads.
# Set coalesce_window_s=0 (or negative) to disable coalescing entirely — this
# is the de-facto rollback knob.
DEFAULT_COALESCE_WINDOW_SECONDS = 0.25

# Background batcher poll interval (P21 step 2).  The background thread wakes
# at this interval to check pending depth even when the depth threshold is not
# yet reached.  Short enough to keep drain responsive, long enough to avoid
# busy-waiting.
_BACKGROUND_POLL_INTERVAL_S = 0.5


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
        coalesce_window_s: Optional[float] = None,
        background_batching: bool = False,
        background_pending_cap: int = 10_000,
        background_drain_timeout_s: float = 60.0,
        metadata_sink: Optional[Callable[[Dict[str, Any]], None]] = None,
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

        # Coalescing window: None → use default constant; 0 or negative → disabled.
        if coalesce_window_s is None:
            self.coalesce_window_s = DEFAULT_COALESCE_WINDOW_SECONDS
        else:
            self.coalesce_window_s = float(coalesce_window_s)

        # Background batching (P21 step 2, D-03.10).  Active only when the
        # dater is enabled AND transport is graphql (the batcher dispatches
        # GraphQL aliases); otherwise normalized OFF with a loud note
        # (fail-open: dates are less important than files).
        self.background_batching = bool(background_batching)
        if self.background_batching and not (
            self.enabled and self.transport == "graphql"
        ):
            logger.warning(
                "[file_dating] background_batching requested but inactive "
                "(requires file_dating.enabled=true and transport=graphql); "
                "ignoring"
            )
            self.background_batching = False
        self.background_pending_cap = max(1, int(background_pending_cap))
        self.background_drain_timeout_s = float(background_drain_timeout_s)
        self._metadata_sink = metadata_sink

        self._lock = threading.Lock()
        self._counters_lock = threading.Lock()
        self._cached_hits = 0
        self._undateable_links = 0
        self._requests_this_run = 0
        self._budget_stopped = 0
        self._time_stopped = 0

        # Assembly state for coalescing (P21).  Only used when coalescing is
        # enabled and transport is graphql.
        self._assembly: Optional[Dict[str, Any]] = None
        self._assembly_cond = threading.Condition(self._lock)

        # Background batching state (P21 step 2).
        # _pending: dict mapping dedup key (url) -> (owner, repo, path, ref)
        self._pending: Dict[str, Tuple[str, str, str, str]] = {}
        self._pending_cond = threading.Condition(threading.Lock())
        self._closing_event = threading.Event()
        self._bg_thread: Optional[threading.Thread] = None
        self._bg_started = False
        # Counters for background mode.
        self._pending_in = 0       # unique keys accepted
        self._dated = 0            # results delivered (filled + absent)
        self._pending_dropped = 0  # keys dropped (cap or drain timeout)
        self._batches_dispatched = 0
        self._warned_full = False  # loud-once guard for cap WARNING

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
            # P21: opportunistic coalescing for small calls.
            if (
                self.coalesce_window_s > 0
                and len(stale) < self.graphql_batch_size
            ):
                self._fetch_graphql_coalesced(stale, flush, deadline=deadline, result_map=result_map)
            else:
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

    # -- background batching (P21 step 2, D-03.10) -----------------------
    def submit_urls(self, urls: Iterable[str]) -> int:
        """Submit URLs for background dating (P21 step 2).

        O(1) insertion per URL into the in-RAM pending set.  Never blocks the
        caller and never raises (fail-open: dates are less important than files).
        Dedup: same key (url) submitted twice does NOT increment pending_in
        twice.  Returns the number of URLs accepted (unique new insertions).

        When the pending set reaches ``background_pending_cap``, new submissions
        are discarded with a loud-once WARNING and ``dating_pending_dropped``
        counter increment.
        """
        if not self.enabled or not self.background_batching:
            return 0

        accepted = 0
        for url in urls or []:
            parsed = _parse_blob_link(url)
            if parsed is None:
                with self._counters_lock:
                    self._undateable_links += 1
                continue
            key = url  # dedup by url
            with self._pending_cond:
                if key in self._pending:
                    # Duplicate: do not increment pending_in.
                    continue
                if len(self._pending) >= self.background_pending_cap:
                    # Cap reached: drop with loud-once WARNING.
                    with self._counters_lock:
                        self._pending_dropped += 1
                    if not self._warned_full:
                        self._warned_full = True
                        logger.warning(
                            "[file_dating] pending cap reached (%d); "
                            "dates are less important than files — dropping new submissions",
                            self.background_pending_cap,
                        )
                    continue
                self._pending[key] = (
                    parsed["owner"],
                    parsed["repo"],
                    parsed["path"],
                    parsed["ref"],
                )
                with self._counters_lock:
                    self._pending_in += 1
                accepted += 1
                self._pending_cond.notify_all()

            # Lazy start of the background thread on first successful submit.
            if not self._bg_started:
                self._ensure_bg_thread()

        return accepted

    def _ensure_bg_thread(self) -> None:
        """Start the background batching thread if not already running."""
        with self._pending_cond:
            if self._bg_started:
                return
            self._bg_started = True
            t = threading.Thread(
                target=self._bg_worker,
                name="file-dating-bg",
                daemon=True,
            )
            self._bg_thread = t
            t.start()

    def _bg_worker(self) -> None:
        """Background batching loop (P21 step 2).

        Waits until pending depth >= graphql_batch_size OR closing event, then
        atomically takes up to graphql_batch_size items and dispatches them.
        On close, drains remaining items within background_drain_timeout_s.
        """
        try:
            while True:
                batch = self._bg_take_batch(blocking=True)
                if batch is None:
                    # Closing event set and pending is empty.
                    break
                self._bg_dispatch_batch(batch)

            # Drain phase: after closing, drain remaining within timeout.
            self._bg_drain()
        except Exception as exc:
            # Fail-open: the thread must not kill the process.
            logger.warning(
                "[file_dating] background thread died (fail-open): %s", exc
            )

    def _bg_take_batch(self, blocking: bool) -> Optional[List[Tuple[str, str, str, str, str]]]:
        """Take up to graphql_batch_size items from pending.

        Returns None when closing and pending is empty.
        """
        with self._pending_cond:
            while len(self._pending) < self.graphql_batch_size:
                if self._closing_event.is_set():
                    if not self._pending:
                        return None
                    # Closing with remaining items: take what we have.
                    break
                self._pending_cond.wait(timeout=_BACKGROUND_POLL_INTERVAL_S)

            # Take up to graphql_batch_size items.
            keys = list(self._pending.keys())[:self.graphql_batch_size]
            batch: List[Tuple[str, str, str, str, str]] = []
            for k in keys:
                owner, repo, path, ref = self._pending.pop(k)
                batch.append((k, owner, repo, path, ref))
            return batch

    def _bg_dispatch_batch(
        self, batch: List[Tuple[str, str, str, str, str]]
    ) -> None:
        """Dispatch one batch through the GraphQL client and deliver results."""
        items = [(o, r, p, ref) for _, o, r, p, ref in batch if ref and len(ref) == 40]

        if not items:
            # Items without usable shas: drop them.
            with self._counters_lock:
                self._pending_dropped += len(batch)
            return

        credential = self._next_credential()
        if credential is None:
            # No credential: drop the batch.
            with self._counters_lock:
                self._pending_dropped += len(batch)
            return

        try:
            results = fetch_file_commit_dates_batch(
                items,
                credential=credential,
                client=self._client,
                timeout=self.timeout,
                retries=self.retries,
            )
        except Exception as exc:
            # Fail-open: drop the batch, increment refusals if the client did.
            logger.debug("[file_dating] background batch failed (fail-open): %s", exc)
            with self._counters_lock:
                self._pending_dropped += len(batch)
            return

        with self._counters_lock:
            self._requests_this_run += 1
            self._batches_dispatched += 1

        if not results:
            # Total failure without exception (e.g. credential exhaustion
            # returns {}): account the whole batch as dropped so the balance
            # dated + dropped == pending_in stays exact (D-03.5).
            with self._counters_lock:
                self._pending_dropped += len(batch)
            return

        # Deliver per BATCH ENTRY via key lookup — never by index alignment:
        # the client dict is keyed by (owner, repo, path) and duplicate keys
        # collapse (last-wins), so enumerate()+idx would misattribute dates
        # across URLs of the same batch.  Every taken entry is accounted:
        # delivered (dated) or undeliverable (dropped) — balance invariant.
        delivered = 0
        undeliverable = 0
        applied_keys = set()
        for url, owner, repo, path, ref in batch:
            key = (owner, repo, path)
            result = results.get(key)
            if result is None or not (ref and len(ref) == 40):
                undeliverable += 1
                continue
            if key not in applied_keys:
                applied_keys.add(key)
                # Apply to cache (FileMetaStore) once per unique file key.
                self._apply_result(owner, repo, path, ref, result, flush=False)
            # Deliver through metadata sink if available (URL-keyed channel,
            # monotonic MAX-merge downstream — D-03.10 p.3).
            if self._metadata_sink is not None:
                try:
                    from core.models import LinkMetadata
                    meta = LinkMetadata(file_commit_date=result.committed_date)
                    self._metadata_sink({url: meta})
                except Exception as exc:
                    logger.debug("[file_dating] metadata sink failed (fail-open): %s", exc)
            delivered += 1

        with self._counters_lock:
            self._dated += delivered
            self._pending_dropped += undeliverable

    def _bg_drain(self) -> None:
        """Drain remaining pending items within the bounded timeout."""
        deadline = time.monotonic() + self.background_drain_timeout_s
        while self._pending:
            if time.monotonic() >= deadline:
                break
            with self._pending_cond:
                keys = list(self._pending.keys())[:self.graphql_batch_size]
                batch: List[Tuple[str, str, str, str, str]] = []
                for k in keys:
                    owner, repo, path, ref = self._pending.pop(k)
                    batch.append((k, owner, repo, path, ref))
            if batch:
                self._bg_dispatch_batch(batch)

        # Drop any remaining items after timeout.
        with self._pending_cond:
            remaining = len(self._pending)
            self._pending.clear()
        if remaining > 0:
            with self._counters_lock:
                self._pending_dropped += remaining
            logger.warning(
                "[file_dating] drain timeout reached; dropped %d undrained items",
                remaining,
            )

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
                # Background batching counters (P21 step 2).
                "pending_size": len(self._pending),
                "pending_in": self._pending_in,
                "dated": self._dated,
                "dating_pending_dropped": self._pending_dropped,
                "batches_dispatched": self._batches_dispatched,
            }

    def close(self) -> None:
        """Flush and close the underlying store (mirrors ``RepoMetaEnricher.close``).

        When background batching is active, signals the background thread to
        drain and joins it with a bounded timeout.  Idempotent.
        """
        # Signal the background thread to stop and drain.
        if self._bg_started:
            self._closing_event.set()
            with self._pending_cond:
                self._pending_cond.notify_all()
            if self._bg_thread is not None:
                # Bounded join: drain timeout + slack.
                join_timeout = self.background_drain_timeout_s + 5.0
                self._bg_thread.join(timeout=join_timeout)

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

    def _fetch_graphql_coalesced(
        self,
        stale: List[Tuple[str, str, str, str, str]],
        flush: bool,
        deadline: Optional[float] = None,
        result_map: Optional[Dict[str, Optional[float]]] = None,
    ) -> int:
        """Coalesce small concurrent GraphQL calls into a shared batch (P21).

        Leader/follower pattern: the first caller creates an assembly and becomes
        the leader; subsequent callers join as followers.  The leader waits for
        the coalesce window or until the assembly reaches batch_size, then
        executes the batch and distributes results.  Followers wait on an event
        and read their own results.

        Failure handling: the leader always sets the event in a finally block,
        so followers are released even if the network call raises.  Followers
        receive None for urls that got no date.
        """
        # Identify which urls this caller owns.
        my_urls = {url for url, o, r, p, _ in stale}

        # Check budget before entering the assembly.
        if self._budget_exhausted():
            with self._counters_lock:
                self._budget_stopped += len(stale)
            return 0

        if self._time_exhausted(deadline):
            with self._counters_lock:
                self._time_stopped += 1
            return 0

        # Enter the assembly under the lock.
        with self._assembly_cond:
            # If no assembly or the current one is closed, create a new one.
            if self._assembly is None or self._assembly["closed"]:
                self._assembly = {
                    "items": [],  # List of (url, owner, repo, path, ref)
                    "keys": set(),  # Set of (owner, repo, path) for dedup
                    "results": {},  # Dict[url, Optional[float]]
                    "event": threading.Event(),
                    "closed": False,
                    "leader_done": False,
                }
                is_leader = True
            else:
                is_leader = False

            # Add this caller's urls to the assembly (dedup by key).
            assembly = self._assembly
            for item in stale:
                url, owner, repo, path, ref = item
                key = (owner, repo, path)
                if key not in assembly["keys"]:
                    assembly["items"].append(item)
                    assembly["keys"].add(key)
            # Wake the leader: a full assembly is the only thing it waits for.
            self._assembly_cond.notify_all()

            if is_leader:
                # Leader: will wait for window outside the lock, then snapshot.
                pass
            else:
                # Follower: wait for the leader to finish.
                # Compute bounded join timeout: window + client timeout + slack.
                join_timeout = self.coalesce_window_s + self.timeout + 2.0
                # Wait for leader to signal completion.
                deadline_time = self._clock() + join_timeout
                while not assembly["leader_done"]:
                    remaining = deadline_time - self._clock()
                    if remaining <= 0:
                        break
                    self._assembly_cond.wait(timeout=min(remaining, 0.5))
                # Read results for my urls.
                if result_map is not None:
                    for url in my_urls:
                        if url in assembly["results"]:
                            result_map[url] = assembly["results"][url]
                # Count filled for this caller.
                filled = sum(
                    1 for url in my_urls
                    if assembly["results"].get(url) is not None
                )
                return filled

        # Leader path: hold the assembly open for the coalesce window so that
        # concurrent gather workers join it, but wake as soon as the assembly
        # already holds a full batch -- there is nothing left to coalesce and
        # the caller is waiting synchronously inside a gather worker (P21).
        window_deadline = time.monotonic() + self.coalesce_window_s
        with self._assembly_cond:
            while len(assembly["items"]) < self.graphql_batch_size:
                remaining = window_deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._assembly_cond.wait(timeout=remaining)

        # Re-acquire lock to close the assembly and snapshot.
        with self._assembly_cond:
            assembly["closed"] = True
            snapshot_items = list(assembly["items"])
            # Notify any followers that might be waiting.
            self._assembly_cond.notify_all()

        # Execute the batch outside the lock.
        try:
            dated = self._execute_coalesced_batch(snapshot_items, flush, deadline, result_map, assembly)
        except Exception as exc:
            # Fail-open: release followers with None.
            logger.debug(f"[file_dating] Coalesced batch failed (fail-open): {exc}")
            dated = 0
        finally:
            # Signal followers that we're done.
            with self._assembly_cond:
                assembly["leader_done"] = True
                self._assembly_cond.notify_all()

        return dated

    def _execute_coalesced_batch(
        self,
        items: List[Tuple[str, str, str, str, str]],
        flush: bool,
        deadline: Optional[float],
        result_map: Optional[Dict[str, Optional[float]]],
        assembly: Dict[str, Any],
    ) -> int:
        """Execute the coalesced batch and populate assembly results."""
        dated = 0

        # Check budget again before executing.
        if self._budget_exhausted():
            with self._counters_lock:
                self._budget_stopped += len(items)
            return 0

        if self._time_exhausted(deadline):
            with self._counters_lock:
                self._time_stopped += 1
            return 0

        # Cooling probe
        if self._cooling_probe is not None:
            try:
                if bool(self._cooling_probe()):
                    return 0
            except Exception:
                pass

        # Build the batch items for the GraphQL call.
        batch_items = [(o, r, p, ref) for _, o, r, p, ref in items if ref and len(ref) == 40]
        batch_urls = [url for url, o, r, p, ref in items if ref and len(ref) == 40]

        if not batch_items:
            return 0

        # Credential rotation loop.
        for _ in range(self.max_credential_retries):
            credential = self._next_credential()
            if credential is None:
                break
            try:
                results = fetch_file_commit_dates_batch(
                    batch_items,
                    credential=credential,
                    client=self._client,
                    timeout=self.timeout,
                    retries=self.retries,
                )
            except GithubCredentialLimited:
                continue
            except Exception as exc:
                logger.debug(f"[file_dating] Coalesced GraphQL batch failed (fail-open): {exc}")
                break

            with self._counters_lock:
                self._requests_this_run += 1

            # Distribute results to assembly and result_map.
            for idx, ((owner, repo, path), result) in enumerate(results.items()):
                ref = ""
                for o, r, p, r_ref in batch_items:
                    if (o, r, p) == (owner, repo, path):
                        ref = r_ref
                        break
                self._apply_result(owner, repo, path, ref, result, flush)
                if result.status == 200 and result.committed_date is not None:
                    dated += 1
                    # Find the URL for this (owner, repo, path).
                    if idx < len(batch_urls):
                        url = batch_urls[idx]
                        assembly["results"][url] = result.committed_date
                        if result_map is not None:
                            result_map[url] = result.committed_date
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
