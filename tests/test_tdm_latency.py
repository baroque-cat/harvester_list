"""Bounded-memory latency percentiles per transport.

Traceability: gather-transport-S27, S28; run-observability-RO-S9, RO-S10.

Contract under test (design D6): fetch latency is accumulated into a fixed set
of millisecond bands per transport kind, and ``get_gather_transport_stats()``
derives ``latency_samples_<kind>``, ``latency_p50_ms_<kind>`` and
``latency_p99_ms_<kind>`` from it, reporting the **upper edge** of the band that
contains the target rank so a published percentile never understates the measured
one.  Band edges are chosen so acceptance row A2 is decidable: they separate the
captured raw band (240-340 ms; probe 2026-09-25, raw.githubusercontent.com,
anonymous, p50 338 ms / p99 390 ms) and they separate 750 ms from 1000 ms, which
is the "p99 < 1 s" criterion.

RED at plan time: no latency instrumentation exists in ``search/client.py``
(``grep -n "latency|elapsed|perf_counter|monotonic"`` returns nothing), so
``_LATENCY_BAND_EDGES_MS`` / ``_gather_latency_observe`` are missing
(``AttributeError``) and no ``latency_*`` key is published (``KeyError``).
"""

import pytest

from search import client as sc

SHA = "a" * 40
BLOB = f"https://github.com/owner/repo/blob/{SHA}/src/f.txt"
KINDS = ("raw", "html", "rest")


class _FakeResponse:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    def raise_for_status(self):
        if not self.ok:
            import requests

            raise requests.HTTPError(f"{self.status_code}", response=self)

    def iter_content(self, chunk_size=8192):
        data = self.content
        for i in range(0, len(data), chunk_size):
            yield data[i : i + chunk_size]


class _FakeSession:
    def __init__(self, responses=()):
        self._responses = list(responses)
        self.calls = []

    def request(self, method=None, url=None, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        r = self._responses.pop(0) if self._responses else _FakeResponse(200, "x")
        return r() if callable(r) else r

    def get(self, url, **kwargs):
        return self.request(method="GET", url=url, **kwargs)


@pytest.fixture
def live_client(monkeypatch):
    """A real client over a stubbed session with an unconfigured limiter."""
    from tools.ratelimit import RateLimiter

    sess = _FakeSession([_FakeResponse(200, "APP_KEY=hello") for _ in range(8)])
    gh = sc.GitHubClient(limiter=RateLimiter({}))
    monkeypatch.setattr(sc, "_HTTP_SESSION", sess)
    monkeypatch.setattr(sc, "_github_client", gh)
    sc.reset_gather_transport_stats()
    return gh, sess


def _published_keys():
    return set(sc.get_gather_transport_stats())


# ---------------------------------------------------------------------------
# gather-transport-S27
# ---------------------------------------------------------------------------
def test_s27_latency_percentiles_are_published_per_transport(live_client):
    """WHEN a run completes with gather traffic on one transport THEN status
    exposes, for that transport, the number of timed fetches and their 50th and
    99th percentile latency in milliseconds, while the transports that carried no
    traffic stay at zero.
    """
    gh, sess = live_client

    for _ in range(3):
        out = sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5.0)
        assert out == "APP_KEY=hello"

    stats = sc.get_gather_transport_stats()
    assert stats["requests_raw"] == 3
    assert stats["latency_samples_raw"] == 3, "every completed network fetch is timed"
    for key in ("latency_p50_ms_raw", "latency_p99_ms_raw"):
        assert isinstance(stats[key], int), f"{key} must be an integer count of milliseconds"
        # a stubbed session answers in well under the first band, whose upper
        # edge is the conservative published value
        assert stats[key] == sc._LATENCY_BAND_EDGES_MS[0]
    assert stats["latency_p99_ms_raw"] >= stats["latency_p50_ms_raw"]
    for kind in ("html", "rest"):
        assert stats[f"latency_samples_{kind}"] == 0
        assert stats[f"latency_p50_ms_{kind}"] == 0
        assert stats[f"latency_p99_ms_{kind}"] == 0


# ---------------------------------------------------------------------------
# gather-transport-S28
# ---------------------------------------------------------------------------
def test_s28_latency_summary_costs_bounded_memory():
    """WHEN the number of gathered files grows by orders of magnitude THEN the
    memory held by the latency summary is unchanged, because it accumulates into
    a fixed set of millisecond bands rather than retaining per-request samples.
    """
    sc.reset_gather_transport_stats()
    keys_before = _published_keys()
    bands_before = {kind: len(counts) for kind, counts in sc._GATHER_LATENCY_BANDS.items()}

    for i in range(20_000):
        sc._gather_latency_observe("raw", float(i % 4000))

    stats = sc.get_gather_transport_stats()
    assert stats["latency_samples_raw"] == 20_000
    assert _published_keys() == keys_before, "the published surface must not grow with sample count"
    assert {kind: len(counts) for kind, counts in sc._GATHER_LATENCY_BANDS.items()} == bands_before
    assert set(bands_before) == set(KINDS)
    assert bands_before["raw"] == len(sc._LATENCY_BAND_EDGES_MS)


# ---------------------------------------------------------------------------
# run-observability-RO-S9
# ---------------------------------------------------------------------------
def test_ro_s9_percentiles_come_from_the_band_containing_the_rank():
    """WHEN a known set of fetch latencies is recorded across the fixed bands
    THEN the published percentiles are the upper edges of the bands containing
    those ranks - at or above the true percentile, never below it.

    Distribution: 90 samples at 10 ms, 9 at 300 ms, 1 at 5000 ms (n = 100).
    True p50 = 10 ms, true p99 = 300 ms.
    """
    sc.reset_gather_transport_stats()

    for _ in range(90):
        sc._gather_latency_observe("raw", 10.0)
    for _ in range(9):
        sc._gather_latency_observe("raw", 300.0)
    sc._gather_latency_observe("raw", 5000.0)

    stats = sc.get_gather_transport_stats()
    edges = list(sc._LATENCY_BAND_EDGES_MS)
    assert stats["latency_samples_raw"] == 100

    p50 = stats["latency_p50_ms_raw"]
    p99 = stats["latency_p99_ms_raw"]

    # conservative: never below the true percentile
    assert p50 >= 10 and p99 >= 300
    # and exactly the band upper edge containing the rank
    assert p50 == next(e for e in edges if e >= 10)
    assert p99 == next(e for e in edges if e >= 300)
    # the published bands are fine enough to decide the A2 criteria
    assert 250 in edges and 300 in edges and 350 in edges, "the captured raw band must be separable"
    assert 750 in edges and 1000 in edges, "'p99 < 1 s' must be decidable from the published bands"


# ---------------------------------------------------------------------------
# run-observability-RO-S10
# ---------------------------------------------------------------------------
def test_ro_s10_reset_clears_the_latency_distribution():
    """WHEN the transport metric surface is reset between runs THEN sample counts
    return to zero and published percentiles report no data, so a fresh run cannot
    inherit the previous run's latency distribution.
    """
    sc.reset_gather_transport_stats()
    for _ in range(25):
        sc._gather_latency_observe("raw", 120.0)
    assert sc.get_gather_transport_stats()["latency_samples_raw"] == 25

    sc.reset_gather_transport_stats()

    stats = sc.get_gather_transport_stats()
    for kind in KINDS:
        assert stats[f"latency_samples_{kind}"] == 0
        assert stats[f"latency_p50_ms_{kind}"] == 0
        assert stats[f"latency_p99_ms_{kind}"] == 0
    assert all(sum(counts) == 0 for counts in sc._GATHER_LATENCY_BANDS.values())
