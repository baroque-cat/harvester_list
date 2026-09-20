"""Offline hardening under aggregation (add-search-aggregation, tasks 5.3/5.4/6.1).

These are implementation-hardening checks, not new spec scenarios: the delta
spec's scenarios are covered 1:1 in the ``test_sa_*`` driver files.  Here we
prove that aggregation composes with the archived ``fix-silent-losses``
guarantees through the real dispatcher seam and a mock GitHub client.

Covered:
- mode matrix ``off|shadow|on`` x duplicate-query pairs: exactly one HTTP call
  per fingerprint under ``on``, N per provider under ``off``/``shadow``;
- transient failure-empties (network error -> blank payload -> limiter
  suppression) are never stored nor served in any mode, ``poisoned_rejected``
  stays 0, and a later fetch still performs a real request;
- a credential-limit storm collapses onto one real request via singleflight
  and propagates the signal verbatim to every waiter (zero tasks lost).
"""

import json
import threading
import time

import pytest

from core.exceptions import TransientFetchError
from search import aggregation
from search import client as search_client
from tools.state import GithubCredentialLimited, github_credential_state

API_ITEM = {
    "html_url": "https://github.com/o/r/blob/main/a.py",
    "repository": {"pushed_at": "2026-09-01T00:00:00Z", "size": 123},
}
API_BODY = json.dumps({"total_count": 1, "incomplete_results": False, "items": [API_ITEM]})
QUERY = '"sk-"'
MODES = ("off", "shadow", "on")


class _CountingFakeClient:
    """Counts real HTTP executions; raises a configured exception when asked."""

    def __init__(self, body="", raise_exc=None, delay=0.0):
        self._body = body
        self._raise = raise_exc
        self._delay = delay
        self.get_calls = 0
        self.report_calls = 0

    def get(self, url, headers=None, params=None, retries=3, interval=0, timeout=10, credential=None):
        self.get_calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._raise:
            raise self._raise
        return self._body

    def get_with_headers(self, *args, **kwargs):
        self.get_calls += 1
        if self._delay:
            time.sleep(self._delay)
        if self._raise:
            raise self._raise
        return self._body, {}

    def _report(self, service, success, credential=None):
        self.report_calls += 1


@pytest.fixture(autouse=True)
def _clean_aggregation():
    aggregation.reset_aggregator()
    github_credential_state._items.clear()
    yield
    aggregation.reset_aggregator()
    github_credential_state._items.clear()


def _configure(mode, workspace=None):
    aggregation.configure_aggregator(
        aggregation.SearchAggregator(
            mode=mode,
            ttl_web_s=120,
            ttl_api_s=300,
            max_bytes=64 * 1024 * 1024,
            join_timeout_s=5,
            workspace=workspace,
        )
    )


# ---------------------------------------------------------------------------
# 6.1 - offline mode matrix: duplicate-query pairs
# ---------------------------------------------------------------------------
def test_mode_matrix_duplicate_pairs_http_counts(monkeypatch, tmp_path):
    expected = {"off": 2, "shadow": 2, "on": 1}
    for mode in MODES:
        fake = _CountingFakeClient(API_BODY)
        monkeypatch.setattr(search_client, "_github_client", fake)
        workspace = str(tmp_path) if mode == "shadow" else None
        _configure(mode, workspace=workspace)

        search_client.search_with_count(QUERY, "tok", 1, with_api=True, peer_page=100)
        search_client.search_with_count(QUERY, "tok", 1, with_api=True, peer_page=100)

        assert fake.get_calls == expected[mode], f"mode={mode}"
        if mode == "shadow":
            log = tmp_path / "aggregation_decisions.jsonl"
            assert log.exists()
            record = json.loads(log.read_text().strip().splitlines()[-1])
            assert record["mode"] == "shadow"
            assert record["jaccard"] == 1.0


# ---------------------------------------------------------------------------
# 5.3 - failure-empties are never admitted nor served, in every mode
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize(
    "failure", ["blank_payload", "network_error"], ids=["limiter-blank", "network"]
)
def test_failure_empties_never_cached_or_served(monkeypatch, mode, failure):
    if failure == "blank_payload":
        # Mirrors limiter suppression: get_with_headers() -> ("", {}) -> get() -> "".
        fake = _CountingFakeClient(body="")
    else:
        fake = _CountingFakeClient(raise_exc=TimeoutError("boom"))
    monkeypatch.setattr(search_client, "_github_client", fake)
    _configure(mode)

    with pytest.raises(TransientFetchError):
        search_client.search_with_count(QUERY, "tok", 1, with_api=True, peer_page=100)

    agg = aggregation.get_aggregator()
    assert agg is None or agg.metrics.entries == 0
    assert agg is None or agg.metrics.poisoned_rejected == 0

    # A later healthy fetch must still perform a real request (no poison served).
    fake._raise = None
    fake._body = API_BODY
    results, total, _content = search_client.search_with_count(QUERY, "tok", 1, with_api=True, peer_page=100)
    assert results == [API_ITEM["html_url"]] and total == 1
    assert fake.get_calls >= 2


# ---------------------------------------------------------------------------
# 5.4 - credential-limit storm collapses via singleflight, verbatim to all
# ---------------------------------------------------------------------------
def test_credential_limit_storm_coalesces_and_propagates(monkeypatch):
    exc = GithubCredentialLimited(service="github_api", credential="tok", wait=60.0)
    fake = _CountingFakeClient(raise_exc=exc, delay=0.2)
    monkeypatch.setattr(search_client, "_github_client", fake)
    _configure("on")

    barrier = threading.Barrier(4, timeout=5)
    errors = []

    def worker():
        barrier.wait()
        try:
            search_client.search_with_count(QUERY, "tok", 1, with_api=True, peer_page=100)
        except BaseException as e:  # noqa: BLE001 - verbatim propagation check
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert len(errors) == 4
    assert all(isinstance(e, GithubCredentialLimited) for e in errors)
    # The burst collapsed onto a single real request (singleflight).
    assert fake.get_calls == 1
    assert aggregation.get_aggregator().metrics.poisoned_rejected == 0
