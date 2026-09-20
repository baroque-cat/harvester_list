"""Shared-response store mechanics (add-search-aggregation).

Traceability: search-aggregation-S7, S9, S12, S13, S14, S16, S21.

Harness: pure in-process tests against the planned ``search/aggregation.py``
(``SearchAggregator``) with an injected fake clock and counting fake fetchers.
No network. Import failure IS the expected RED state.

Contract driven by these tests:
    agg = SearchAggregator(mode, ttl_web_s, ttl_api_s, max_bytes,
                           join_timeout_s, clock, workspace)
    (results, total, content) = agg.call(use_api, query, page, real_fn)
    # real_fn() -> (results: List[str], total: int, content: str)
    # agg.metrics exposes: hits, misses, joins, join_timeouts, evictions,
    #                      entries, bytes, poisoned_rejected, shadow_comparisons
"""

import pytest

from search.aggregation import SearchAggregator  # RED driver: module absent pre-fix


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _fetcher(payload=None, counter=None):
    payload = payload if payload is not None else (["https://github.com/o/r/blob/main/a.py"], 1, "<html/>")

    def real_fn():
        if counter is not None:
            counter.append(1)
        return payload

    return real_fn


def _agg(mode="on", clock=None, **kwargs):
    defaults = dict(ttl_web_s=120, ttl_api_s=300, max_bytes=64 * 1024 * 1024, join_timeout_s=60)
    defaults.update(kwargs)
    return SearchAggregator(mode=mode, clock=clock or FakeClock(), **defaults)


# ---------------------------------------------------------------------------
# search-aggregation-S7
# ---------------------------------------------------------------------------
def test_s7_sequential_identical_fetch_within_ttl_costs_one_http():
    calls = []
    agg = _agg()

    r1 = agg.call(False, "/q/", 1, _fetcher(counter=calls))
    r2 = agg.call(False, "/q/", 1, _fetcher(counter=calls))

    assert len(calls) == 1                     # exactly one real fetch
    assert r1[0] == r2[0] and r1[1] == r1[1] and r2[2] == r1[2]
    assert agg.metrics.misses == 1
    assert agg.metrics.hits == 1

    # different page / query / transport are distinct identities
    agg.call(False, "/q/", 2, _fetcher(counter=calls))
    agg.call(True, "/q/", 1, _fetcher(counter=calls))
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# search-aggregation-S9
# ---------------------------------------------------------------------------
def test_s9_consumers_get_independent_copies():
    agg = _agg()
    stored_payload = (["u1", "u2"], 2, "content-x")

    r1 = agg.call(False, "/q/", 1, _fetcher(payload=stored_payload))
    r1[0].append("mutated-by-consumer-1")

    r2 = agg.call(False, "/q/", 1, _fetcher(payload=stored_payload))
    assert r2[0] == ["u1", "u2"]               # store untouched by consumer mutation
    assert r1[0] is not r2[0]


# ---------------------------------------------------------------------------
# search-aggregation-S12
# ---------------------------------------------------------------------------
def test_s12_legitimate_zero_is_cached():
    calls = []
    zero_payload = ([], 0, '{"total_count":0,"incomplete_results":false,"items":[]}')
    agg = _agg()

    r1 = agg.call(True, '"sk-" AND content:"llm"', 1, _fetcher(payload=zero_payload, counter=calls))
    r2 = agg.call(True, '"sk-" AND content:"llm"', 1, _fetcher(payload=zero_payload, counter=calls))

    assert len(calls) == 1
    assert r1[0] == [] and r2[1] == 0
    assert agg.metrics.hits == 1


# ---------------------------------------------------------------------------
# search-aggregation-S13
# ---------------------------------------------------------------------------
def test_s13_ttl_expiry_forces_real_fetch():
    calls = []
    clock = FakeClock()
    agg = _agg(clock=clock, ttl_web_s=120)

    agg.call(False, "/q/", 1, _fetcher(counter=calls))
    clock.advance(119)
    agg.call(False, "/q/", 1, _fetcher(counter=calls))
    assert len(calls) == 1                     # still fresh

    clock.advance(2)                           # age 121 > ttl
    agg.call(False, "/q/", 1, _fetcher(counter=calls))
    assert len(calls) == 2                     # expired -> real fetch, entry refreshed

    clock.advance(119)
    agg.call(False, "/q/", 1, _fetcher(counter=calls))
    assert len(calls) == 2                     # refreshed entry is fresh again


# ---------------------------------------------------------------------------
# search-aggregation-S14
# ---------------------------------------------------------------------------
def test_s14_byte_cap_evicts_least_recently_used():
    calls_a, calls_b = [], []
    big = "x" * 400                            # ~400 bytes of content each
    agg = _agg(max_bytes=700)                  # room for roughly one big entry

    held = agg.call(False, "/qa/", 1, _fetcher(payload=(["ua"], 1, big), counter=calls_a))
    agg.call(False, "/qb/", 1, _fetcher(payload=(["ub"], 1, big), counter=calls_b))

    assert agg.metrics.evictions >= 1
    assert agg.metrics.bytes <= 700

    # consumer already holding a copy is unaffected by eviction
    assert held[0] == ["ua"] and held[2] == big

    # oldest entry was evicted: fetching it again performs a real request
    agg.call(False, "/qa/", 1, _fetcher(payload=(["ua"], 1, big), counter=calls_a))
    assert len(calls_a) == 2


# ---------------------------------------------------------------------------
# search-aggregation-S16 (off mode) + S21 (ephemerality)
# ---------------------------------------------------------------------------
def test_s16_off_mode_performs_every_real_fetch(tmp_path):
    calls = []
    agg = _agg(mode="off", workspace=str(tmp_path))

    agg.call(False, "/q/", 1, _fetcher(counter=calls))
    agg.call(False, "/q/", 1, _fetcher(counter=calls))

    assert len(calls) == 2
    assert agg.metrics.hits == 0 and agg.metrics.entries == 0
    # off mode creates no workspace artifacts at all
    assert list(tmp_path.iterdir()) == []


def test_s21_restart_starts_cold(tmp_path):
    calls = []
    agg1 = _agg(workspace=str(tmp_path))
    agg1.call(False, "/q/", 1, _fetcher(counter=calls))
    assert len(calls) == 1

    # simulate process restart: brand-new instance, same workspace
    agg2 = _agg(workspace=str(tmp_path))
    agg2.call(False, "/q/", 1, _fetcher(counter=calls))
    assert len(calls) == 2                     # cold miss - nothing persisted
    assert list(tmp_path.iterdir()) == []      # no cache artifact on disk
