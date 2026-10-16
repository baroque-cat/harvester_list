"""P21: opportunistic coalescing (single-flight) of per-file commit-dating GraphQL batches.

When multiple gather threads each call date_urls_map([single_url]) concurrently,
the coalescing logic should merge them into a single GraphQL POST rather than
issuing one POST per file.

No network.
"""

import threading
import time
from typing import Dict, List, Optional, Tuple

import pytest

from search.client import FileDateResult, reset_file_date_stats


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

# 40-hex shas so the GraphQL batch path accepts them.
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
SHA_D = "d" * 40

URL_A = f"https://github.com/octocat/Hello-World/blob/{SHA_A}/src/a.py"
URL_B = f"https://github.com/octocat/Hello-World/blob/{SHA_B}/src/b.py"
URL_C = f"https://github.com/octocat/Hello-World/blob/{SHA_C}/src/c.py"
URL_D = f"https://github.com/octocat/Hello-World/blob/{SHA_D}/src/d.py"


class _FakeStore:
    """Minimal FileMetaStore stand-in — always stale (no cache hits)."""

    def __init__(self):
        self.writes: list = []

    def get(self, owner, repo, path):
        return None

    def is_fresh(self, entry, sha=None, now=None):
        return False

    def record_upsert(self, *a, **kw):
        self.writes.append(("upsert", a, kw))

    def record_link_date(self, *a, **kw):
        self.writes.append(("link_date", a, kw))

    def record_touch(self, *a, **kw):
        pass

    def record_gone(self, *a, **kw):
        pass

    def flush(self, timeout=10.0):
        pass

    def close(self):
        pass


class _FakeAuth:
    def get_token(self):
        return "test-token"


class _RecordingBatch:
    """A fake fetch_file_commit_dates_batch that records calls and returns dates."""

    def __init__(self, dates=None, raise_exc=None, partial_errors=None):
        """
        dates: dict mapping (owner, repo, path) -> epoch float (or None for absent)
        raise_exc: if set, raise this on every call
        partial_errors: set of (owner, repo, path) that should get status=0 results
        """
        self.calls: List[List[Tuple[str, str, str, str]]] = []
        self.dates = dates or {}
        self.raise_exc = raise_exc
        self.partial_errors = partial_errors or set()
        self.lock = threading.Lock()

    def __call__(self, items, credential=None, client=None, timeout=30.0, retries=3):
        with self.lock:
            self.calls.append(list(items))

        if self.raise_exc is not None:
            raise self.raise_exc

        # Increment the module-level stats counter (mirrors real function).
        from search.client import _file_date_stat_inc
        _file_date_stat_inc("file_dating_batches")
        _file_date_stat_inc("file_dating_requests")
        _file_date_stat_inc("file_dating_graphql_cost", 1)

        results = {}
        for owner, repo, path, sha in items:
            key = (owner, repo, path)
            if key in self.partial_errors:
                results[key] = FileDateResult(owner, repo, path, 0, error="simulated")
            else:
                date = self.dates.get(key)
                if date is not None:
                    results[key] = FileDateResult(owner, repo, path, 200, committed_date=date, sha=sha)
                    _file_date_stat_inc("file_dating_filled")
                else:
                    results[key] = FileDateResult(owner, repo, path, 404, sha=sha)
                    _file_date_stat_inc("file_dating_absent")
        return results


def _make_dater(batch_fn, transport="graphql", coalesce_window_s=None,
                graphql_batch_size=4, max_requests_per_run=200, clock=None):
    """Build a FileDater with a fake batch function."""
    from storage.file_dating import FileDater

    store = _FakeStore()
    dater = FileDater(
        store,
        auth=_FakeAuth(),
        enabled=True,
        transport=transport,
        coalesce_window_s=coalesce_window_s,
        graphql_batch_size=graphql_batch_size,
        max_requests_per_run=max_requests_per_run,
        clock=clock or time.time,
    )
    # Monkeypatch the module-level batch function
    import storage.file_dating as fd_module
    dater._batch_fn = batch_fn
    # We'll patch the import in the module
    return dater, fd_module


# ---------------------------------------------------------------------------
# C1: 4 concurrent single-url calls coalesce into ONE POST with 4 items
# ---------------------------------------------------------------------------
def test_p21_c1_four_threads_one_post(monkeypatch):
    """C1: 4 threads calling date_urls_map([url_i]) concurrently produce exactly
    ONE POST containing 4 items, and every caller receives its own date."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
        ("octocat", "Hello-World", "src/c.py"): NOW - 300,
        ("octocat", "Hello-World", "src/d.py"): NOW - 400,
    }
    recorder = _RecordingBatch(dates=dates)

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, clock=lambda: NOW,
    )

    urls = [URL_A, URL_B, URL_C, URL_D]
    results = [None] * 4
    errors = [None] * 4

    def worker(idx):
        try:
            r = dater.date_urls_map([urls[idx]])
            results[idx] = r
        except Exception as e:
            errors[idx] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # No errors
    for i, e in enumerate(errors):
        assert e is None, f"Thread {i} raised: {e}"

    # Exactly one POST
    assert len(recorder.calls) == 1, f"Expected 1 POST, got {len(recorder.calls)}"
    # With 4 items
    assert len(recorder.calls[0]) == 4

    # Every caller got its own date
    for i in range(4):
        assert results[i] is not None
        assert urls[i] in results[i]
        expected_key = ("octocat", "Hello-World", f"src/{chr(ord('a') + i)}.py")
        assert results[i][urls[i]] == dates[expected_key]


# ---------------------------------------------------------------------------
# C2: counters after coalescing
# ---------------------------------------------------------------------------
def test_p21_c2_counters_after_coalescing(monkeypatch):
    """C2: counters after C1: file_dating_requests==1, batches==1, cost==1, filled==4."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
        ("octocat", "Hello-World", "src/c.py"): NOW - 300,
        ("octocat", "Hello-World", "src/d.py"): NOW - 400,
    }
    recorder = _RecordingBatch(dates=dates)

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, clock=lambda: NOW,
    )

    urls = [URL_A, URL_B, URL_C, URL_D]
    results_holder = [None] * 4

    def worker(idx):
        results_holder[idx] = dater.date_urls_map([urls[idx]])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    stats = dater.to_stats()
    assert stats["file_dating_requests"] == 1
    assert stats["file_dating_batches"] == 1
    assert stats["file_dating_graphql_cost"] == 1
    assert stats["file_dating_filled"] == 4


# ---------------------------------------------------------------------------
# C3: coalesce_window_s=0 disables coalescing (rollback knob)
# ---------------------------------------------------------------------------
def test_p21_c3_disabled_coalescing_four_posts(monkeypatch):
    """C3: coalesce_window_s=0 reproduces today's behaviour: 4 POSTs."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
        ("octocat", "Hello-World", "src/c.py"): NOW - 300,
        ("octocat", "Hello-World", "src/d.py"): NOW - 400,
    }
    recorder = _RecordingBatch(dates=dates)

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, coalesce_window_s=0, clock=lambda: NOW,
    )

    urls = [URL_A, URL_B, URL_C, URL_D]
    results_holder = [None] * 4

    def worker(idx):
        results_holder[idx] = dater.date_urls_map([urls[idx]])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # With coalescing disabled, each call issues its own POST
    assert len(recorder.calls) == 4, f"Expected 4 POSTs, got {len(recorder.calls)}"


# ---------------------------------------------------------------------------
# C4: REST transport never coalesces
# ---------------------------------------------------------------------------
def test_p21_c4_rest_never_coalesces(monkeypatch):
    """C4: transport='rest' never coalesces even with a positive window."""
    reset_file_date_stats()

    from storage.file_dating import FileDater, fetch_file_commit_date
    from search.client import FileDateResult

    NOW = 1_700_000_000.0
    rest_calls = []

    def fake_rest(owner, repo, path, sha=None, credential=None, client=None, timeout=30.0, retries=3):
        rest_calls.append((owner, repo, path))
        return FileDateResult(owner, repo, path, 200, committed_date=NOW - 100, sha=sha or "")

    monkeypatch.setattr("storage.file_dating.fetch_file_commit_date", fake_rest)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="rest",
        coalesce_window_s=0.25, clock=lambda: NOW,
    )

    urls = [URL_A, URL_B]
    for url in urls:
        dater.date_urls_map([url])

    # REST: one call per file, no coalescing
    assert len(rest_calls) == 2


# ---------------------------------------------------------------------------
# C5: large batch (>= graphql_batch_size) executes immediately
# ---------------------------------------------------------------------------
def test_p21_c5_large_batch_no_window_wait(monkeypatch):
    """C5: a call carrying >= graphql_batch_size urls executes immediately
    and does not wait the window."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
        ("octocat", "Hello-World", "src/c.py"): NOW - 300,
        ("octocat", "Hello-World", "src/d.py"): NOW - 400,
    }
    recorder = _RecordingBatch(dates=dates)

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, coalesce_window_s=5.0,  # large window
        clock=lambda: NOW,
    )

    # Pass all 4 URLs at once — should NOT wait the 5s window
    urls = [URL_A, URL_B, URL_C, URL_D]
    t0 = time.monotonic()
    result = dater.date_urls_map(urls)
    elapsed = time.monotonic() - t0

    # Should complete well under the window
    assert elapsed < 2.0, f"Large batch waited {elapsed:.2f}s (window was 5.0s)"
    assert len(recorder.calls) == 1
    assert len(recorder.calls[0]) == 4


# ---------------------------------------------------------------------------
# C6: leader failure releases all followers with None
# ---------------------------------------------------------------------------
def test_p21_c6_leader_failure_releases_followers(monkeypatch):
    """C6: leader failure → all followers released, every caller gets None,
    no exception escapes, elapsed time stays below documented join bound."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    recorder = _RecordingBatch(raise_exc=RuntimeError("network down"))

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, clock=lambda: NOW, timeout=5.0,
    )

    urls = [URL_A, URL_B, URL_C, URL_D]
    results_holder = [None] * 4
    errors = [None] * 4

    def worker(idx):
        try:
            results_holder[idx] = dater.date_urls_map([urls[idx]])
        except Exception as e:
            errors[idx] = e

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.monotonic() - t0

    # No exception escaped
    for i, e in enumerate(errors):
        assert e is None, f"Thread {i} raised: {e}"

    # Every caller got None for its url
    for i in range(4):
        assert results_holder[i] is not None
        assert urls[i] in results_holder[i]
        assert results_holder[i][urls[i]] is None

    # Elapsed time bounded: window (0.25) + timeout (5.0) + slack (2.0) = 7.25
    assert elapsed < 10.0, f"Followers waited {elapsed:.2f}s (bound ~7.25s)"


# ---------------------------------------------------------------------------
# C7: partial errors — some aliases missing, others dated
# ---------------------------------------------------------------------------
def test_p21_c7_partial_errors(monkeypatch):
    """C7: partial errors inside a 200 → affected urls None, others dated."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
        ("octocat", "Hello-World", "src/c.py"): NOW - 300,
        ("octocat", "Hello-World", "src/d.py"): NOW - 400,
    }
    # b and d will have errors
    recorder = _RecordingBatch(
        dates=dates,
        partial_errors={("octocat", "Hello-World", "src/b.py"), ("octocat", "Hello-World", "src/d.py")},
    )

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, clock=lambda: NOW,
    )

    urls = [URL_A, URL_B, URL_C, URL_D]
    results_holder = [None] * 4

    def worker(idx):
        results_holder[idx] = dater.date_urls_map([urls[idx]])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # a and c got dates
    assert results_holder[0][URL_A] == NOW - 100
    assert results_holder[2][URL_C] == NOW - 300
    # b and d got None
    assert results_holder[1][URL_B] is None
    assert results_holder[3][URL_D] is None


# ---------------------------------------------------------------------------
# C8: duplicate URL requested by two threads → one POST entry, both get value
# ---------------------------------------------------------------------------
def test_p21_c8_duplicate_url_single_post_entry(monkeypatch):
    """C8: same url requested by two threads at once → one POST entry, both get value."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
    }
    recorder = _RecordingBatch(dates=dates)

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, clock=lambda: NOW,
    )

    results_holder = [None, None]

    def worker(idx):
        results_holder[idx] = dater.date_urls_map([URL_A])

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    # Both got the value
    assert results_holder[0][URL_A] == NOW - 100
    assert results_holder[1][URL_A] == NOW - 100

    # Only one item in the POST (deduplicated)
    assert len(recorder.calls) == 1
    assert len(recorder.calls[0]) == 1


# ---------------------------------------------------------------------------
# C9: max_requests_per_run ceiling reached → waiters released with None
# ---------------------------------------------------------------------------
def test_p21_c9_ceiling_released_with_none(monkeypatch):
    """C9: max_requests_per_run ceiling reached while callers waiting →
    waiters released with None, budget_stop incremented, no hang."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    recorder = _RecordingBatch(dates={})

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    # Allow only 1 request
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, max_requests_per_run=1, clock=lambda: NOW,
    )

    # Use up the one allowed request
    dater._requests_this_run = 1

    urls = [URL_A, URL_B]
    results_holder = [None, None]
    errors = [None, None]

    def worker(idx):
        try:
            results_holder[idx] = dater.date_urls_map([urls[idx]])
        except Exception as e:
            errors[idx] = e

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    for i, e in enumerate(errors):
        assert e is None, f"Thread {i} raised: {e}"

    # Both got None (ceiling reached)
    for i in range(2):
        assert results_holder[i] is not None
        assert urls[i] in results_holder[i]
        assert results_holder[i][urls[i]] is None

    stats = dater.to_stats()
    assert stats["file_dating_budget_stopped"] >= 2


# ---------------------------------------------------------------------------
# C10: caller arriving after assembly closed starts new batch
# ---------------------------------------------------------------------------
def test_p21_c10_late_caller_gets_new_batch(monkeypatch):
    """C10: a caller arriving after the assembly was closed starts/joins a NEW batch."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
    }
    recorder = _RecordingBatch(dates=dates)

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, coalesce_window_s=0.1, clock=lambda: NOW,
    )

    # First call — will create and close an assembly
    r1 = dater.date_urls_map([URL_A])
    assert r1[URL_A] == NOW - 100

    # Second call — should create a NEW assembly/batch
    r2 = dater.date_urls_map([URL_B])
    assert r2[URL_B] == NOW - 200

    # Two separate POSTs
    assert len(recorder.calls) == 2


# ---------------------------------------------------------------------------
# C11: no caller waits longer than documented bound even if leader dies
# ---------------------------------------------------------------------------
def test_p21_c11_bounded_wait_on_leader_death(monkeypatch):
    """C11: no caller waits longer than the documented bound even if the leader
    thread dies before setting the event — the finally must still release followers."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0

    call_count = [0]

    def dying_batch(items, credential=None, client=None, timeout=30.0, retries=3):
        call_count[0] += 1
        raise RuntimeError("leader died")

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", dying_batch)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, clock=lambda: NOW, timeout=5.0,
    )

    urls = [URL_A, URL_B, URL_C, URL_D]
    results_holder = [None] * 4
    errors = [None] * 4

    def worker(idx):
        try:
            results_holder[idx] = dater.date_urls_map([urls[idx]])
        except Exception as e:
            errors[idx] = e

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.monotonic() - t0

    # No exception escaped
    for i, e in enumerate(errors):
        assert e is None, f"Thread {i} raised: {e}"

    # All got None
    for i in range(4):
        assert results_holder[i] is not None
        assert results_holder[i][urls[i]] is None

    # Bounded wait: window (0.25) + timeout (5.0) + slack (2.0) = 7.25
    assert elapsed < 10.0, f"Waited {elapsed:.2f}s, bound ~7.25s"


# ---------------------------------------------------------------------------
# C12: public contract unchanged
# ---------------------------------------------------------------------------
def test_p21_c12_public_contract_unchanged(monkeypatch):
    """C12: keys of returned map are exactly the input urls, values Optional[float];
    date_urls() still returns the count of non-None."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
    }
    recorder = _RecordingBatch(dates=dates)

    from storage.file_dating import FileDater
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    store = _FakeStore()
    dater = FileDater(
        store, auth=_FakeAuth(), enabled=True, transport="graphql",
        graphql_batch_size=4, clock=lambda: NOW,
    )

    urls = [URL_A, URL_B]
    result = dater.date_urls_map(urls)

    # Keys are exactly the input urls
    assert set(result.keys()) == set(urls)
    # Values are Optional[float]
    for v in result.values():
        assert v is None or isinstance(v, (int, float))

    # date_urls returns count of non-None
    count = dater.date_urls(urls)
    assert count == 2


# ---------------------------------------------------------------------------
# C13: a full assembly wakes the leader early instead of idling the window
# ---------------------------------------------------------------------------
def test_p21_c13_full_assembly_wakes_leader_early(monkeypatch):
    """C13: the leader holds the assembly open for at most the coalesce window,
    but wakes as soon as the batch is full -- a gather worker waits
    synchronously, so idling the remaining window would be pure latency."""
    reset_file_date_stats()

    NOW = 1_700_000_000.0
    dates = {
        ("octocat", "Hello-World", "src/a.py"): NOW - 100,
        ("octocat", "Hello-World", "src/b.py"): NOW - 200,
    }
    recorder = _RecordingBatch(dates=dates)
    monkeypatch.setattr("storage.file_dating.fetch_file_commit_dates_batch", recorder)

    # A deliberately generous window: if the leader slept all of it, this test
    # would take >= 3 s.  With batch_size=2 and two callers it must wake early.
    dater, _ = _make_dater(recorder, transport="graphql",
                           coalesce_window_s=3.0, graphql_batch_size=2)

    results: Dict[int, Dict[str, Optional[float]]] = {}
    barrier = threading.Barrier(2)

    def worker(idx, url):
        barrier.wait()
        results[idx] = dater.date_urls_map([url])

    urls = [URL_A, URL_B]
    threads = [threading.Thread(target=worker, args=(i, urls[i])) for i in range(2)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.monotonic() - started

    assert len(recorder.calls) == 1, f"expected ONE POST, got {len(recorder.calls)}"
    assert len(recorder.calls[0]) == 2
    assert results[0][URL_A] == pytest.approx(NOW - 100)
    assert results[1][URL_B] == pytest.approx(NOW - 200)
    # Woken early: far below the 3.0 s window, with slack for CI scheduling.
    assert elapsed < 2.0, f"leader idled the window: {elapsed:.2f}s"
    dater.close()
