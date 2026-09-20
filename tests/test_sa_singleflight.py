"""Singleflight coalescing semantics (add-search-aggregation).

Traceability: search-aggregation-S8, S11, S15.

Harness: threads with barriers against the planned ``search/aggregation.py``;
fake fetchers with controlled latency and exceptions. No network. Import
failure IS the expected RED state.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.exceptions import TransientFetchError
from search.aggregation import SearchAggregator  # RED driver: module absent pre-fix
from tools.state import GithubCredentialLimited


def _agg(**kwargs):
    defaults = dict(mode="on", ttl_web_s=120, ttl_api_s=300,
                    max_bytes=64 * 1024 * 1024, join_timeout_s=5)
    defaults.update(kwargs)
    return SearchAggregator(**defaults)


PAYLOAD = (["u1", "u2"], 2, "<html/>")


# ---------------------------------------------------------------------------
# search-aggregation-S8
# ---------------------------------------------------------------------------
def test_s8_concurrent_identical_fetches_coalesce():
    calls = []
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)

    def slow_real():
        with lock:
            calls.append(1)
        time.sleep(0.2)
        return PAYLOAD

    agg = _agg()

    def worker():
        barrier.wait()
        return agg.call(False, "/q/", 1, slow_real)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: worker(), range(3)))

    assert len(calls) == 1                          # exactly one HTTP request
    assert all(r[0] == PAYLOAD[0] and r[1] == PAYLOAD[1] for r in results)
    assert agg.metrics.joins >= 2                   # two waiters rode the leader


# ---------------------------------------------------------------------------
# search-aggregation-S11
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("exc_factory", [
    lambda: TransientFetchError("network down"),
    lambda: GithubCredentialLimited(service="github_web", credential="s1", wait=60.0),
], ids=["transient", "credential-limited"])
def test_s11_leader_failure_propagates_verbatim_and_stores_nothing(exc_factory):
    calls = []
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)

    def failing_real():
        with lock:
            calls.append(1)
        time.sleep(0.15)
        raise exc_factory()

    agg = _agg()
    expected_type = type(exc_factory())
    errors = []

    def worker():
        barrier.wait()
        try:
            agg.call(False, "/q/", 1, failing_real)
        except BaseException as e:               # noqa: BLE001 - verbatim propagation check
            errors.append(e)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda _: worker(), range(3)))

    assert len(errors) == 3
    assert all(isinstance(e, expected_type) for e in errors)
    assert isinstance(errors[1], type(errors[0]))  # same exception class for every joiner
    assert agg.metrics.entries == 0                # nothing stored from a failure

    # next attempt performs a fresh real request (not served from poison)
    with pytest.raises(expected_type):
        agg.call(False, "/q/", 1, failing_real)
    assert len(calls) >= 2


# ---------------------------------------------------------------------------
# search-aggregation-S15
# ---------------------------------------------------------------------------
def test_s15_join_timeout_degrades_to_direct_request():
    calls = []
    lock = threading.Lock()
    started = threading.Event()

    def very_slow_real():
        with lock:
            calls.append(1)
        started.set()
        time.sleep(1.2)                          # far beyond the join timeout
        return PAYLOAD

    agg = _agg(join_timeout_s=0.3)
    results = {}

    def leader():
        results["leader"] = agg.call(False, "/q/", 1, very_slow_real)

    def joiner():
        started.wait(timeout=2)                  # ensure the flight is in progress
        results["joiner"] = agg.call(False, "/q/", 1, very_slow_real)

    t1 = threading.Thread(target=leader)
    t2 = threading.Thread(target=joiner)
    t1.start(); t2.start()
    t2.join(timeout=3); t1.join(timeout=3)

    assert not t2.is_alive(), "joiner must return within the join-timeout bound"
    assert len(calls) == 2                       # joiner fell back to its own request
    assert results["joiner"][0] == PAYLOAD[0]    # completed normally, no failure/drop
    assert agg.metrics.join_timeouts >= 1
