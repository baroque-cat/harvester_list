"""Tri-mode flag, shadow measurement and client wiring (add-search-aggregation).

Traceability: search-aggregation-S17, S18, S19, S10 (+ auxiliary metadata-copy
pin for the API transport out-param).

Harness: planned ``search/aggregation.py`` (SearchAggregator, configure/
reset hooks) plus the wired dispatchers in ``search/client.py`` driven through
a fake GitHubClient singleton. No network. Import failure IS the expected RED
state for aggregation-module imports.
"""

import json

import pytest

from search.aggregation import SearchAggregator, configure_aggregator, reset_aggregator  # RED driver
from search import client as search_client
from tools.state import github_credential_state


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _agg(mode, workspace=None, clock=None):
    return SearchAggregator(mode=mode, ttl_web_s=120, ttl_api_s=300,
                            max_bytes=64 * 1024 * 1024, join_timeout_s=5,
                            workspace=workspace, clock=clock or FakeClock())


# ---------------------------------------------------------------------------
# search-aggregation-S17
# ---------------------------------------------------------------------------
def test_s17_shadow_measures_without_serving(tmp_path):
    clock = FakeClock()
    agg = _agg("shadow", workspace=str(tmp_path), clock=clock)

    live1 = (["u1", "u2", "u3"], 3, "c1")
    r1 = agg.call(False, "/q/", 1, lambda: live1)
    assert r1[0] == ["u1", "u2", "u3"]

    live2 = (["u1", "u2", "u4"], 4, "c2")       # drifted set: Jaccard = 2/4 = 0.5
    r2 = agg.call(False, "/q/", 1, lambda: live2)

    # caller ALWAYS receives the live result under shadow
    assert r2[0] == ["u1", "u2", "u4"] and r2[1] == 4
    assert agg.metrics.shadow_comparisons == 1

    log = tmp_path / "aggregation_decisions.jsonl"
    assert log.exists()
    record = json.loads(log.read_text().strip().splitlines()[-1])
    assert record["mode"] == "shadow"
    assert record["page"] == 1
    assert abs(record["jaccard"] - 0.5) < 1e-9
    assert record["total_delta"] == 1
    assert "fingerprint" in record and "ts" in record


# ---------------------------------------------------------------------------
# search-aggregation-S18 / S19
# ---------------------------------------------------------------------------
def test_s18_s19_on_serves_and_flag_flip_rolls_back():
    calls = []

    def real():
        calls.append(1)
        return (["u1"], 1, "c")

    on_agg = _agg("on")
    on_agg.call(False, "/q/", 1, real)
    on_agg.call(False, "/q/", 1, real)
    assert len(calls) == 1                        # sharing active

    off_agg = _agg("off")                         # same code, flipped configuration
    off_agg.call(False, "/q/", 1, real)
    off_agg.call(False, "/q/", 1, real)
    assert len(calls) == 3                        # rollback: every fetch is real again


# ---------------------------------------------------------------------------
# search-aggregation-S10 (+ metadata copy pin) via the wired client dispatchers
# ---------------------------------------------------------------------------
API_ITEM = {
    "html_url": "https://github.com/o/r/blob/main/a.py",
    "repository": {"pushed_at": "2026-09-01T00:00:00Z", "size": 123},
}
API_BODY = json.dumps({"total_count": 1, "incomplete_results": False, "items": [API_ITEM]})


class _CountingFakeClient:
    """Counts real HTTP executions and mirrors limiter/cooldown touchpoints."""

    def __init__(self, body):
        self._body = body
        self.get_calls = 0
        self.report_calls = 0

    def get(self, url, headers=None, params=None, retries=3, interval=0, timeout=10, credential=None):
        self.get_calls += 1
        return self._body

    def get_with_headers(self, *args, **kwargs):
        self.get_calls += 1
        return self._body, {}

    def _report(self, service, success, credential=None):
        self.report_calls += 1


def test_s10_hit_bypasses_accounting_and_metadata_is_copied(monkeypatch):
    fake = _CountingFakeClient(API_BODY)
    monkeypatch.setattr(search_client, "_github_client", fake)
    github_credential_state._items.clear()

    configure_aggregator(_agg("on"))
    try:
        m1 = {}
        r1 = search_client.search_with_count(
            query='"sk-"', session="tok", page=1, with_api=True, peer_page=100, metadata=m1
        )
        m2 = {}
        r2 = search_client.search_with_count(
            query='"sk-"', session="tok", page=1, with_api=True, peer_page=100, metadata=m2
        )

        assert fake.get_calls == 1                 # second call served from the store
        assert r1[0] == r2[0] and r1[1] == r2[1] == 1

        # I3: no adaptive reporting and no cooldown-state touches attributable to the hit
        assert fake.report_calls <= 1
        assert github_credential_state._items == {}

        # metadata out-param filled per consumer from the snapshot, independent copies
        assert m1 and m2
        assert m1 is not m2
        m2.clear()
        m3 = {}
        search_client.search_with_count(
            query='"sk-"', session="tok", page=1, with_api=True, peer_page=100, metadata=m3
        )
        assert m3                                    # store intact despite consumer mutation
        assert fake.get_calls == 1
    finally:
        reset_aggregator()
        github_credential_state._items.clear()
