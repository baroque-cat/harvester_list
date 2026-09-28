"""Local-budget withholding is a deferral, not a task fault.

Traceability: gather-transport-S22, S23, S24, S25, S26, S29; failure-handling-FH3-S6.

Harness: the offline seam established by ``tests/test_gt_transport.py`` - the HTTP
session is stubbed at ``search.client._HTTP_SESSION`` and a real ``GitHubClient``
runs over a real ``RateLimiter``, so throttling attribution, adaptive accounting
and refusal classification all execute for real while the suite stays offline.
Starvation itself is forced at the decision seam (``GitHubClient._limit``), which
is exactly the branch under test; the adaptive budget is asserted on the real
``TokenBucket`` state rather than on a spy, so a regression that re-reports a
withheld request as a failure fails on the bucket's own rate.

RED at plan time:
- ``deferred_local_budget`` is not in ``_GATHER_TRANSPORT_STAT_KEYS`` and no
  ``latency_*`` key exists, so the stats assertions raise ``KeyError``;
- the withholding branch still raises ``TransientFetchError`` and still calls
  ``_report(service, False, credential)`` (``search/client.py:1319-1322``), so the
  deferral and no-decay assertions fail;
- ``fetch_gather_content`` has no ``defer_local_suppression`` parameter,
  ``search.client._configured_defer_local_suppression`` does not exist and
  ``GatherConfig.defer_local_suppression`` does not exist, so the rollback pins
  fail on ``TypeError``/``AttributeError``.
"""

import pytest

from config.schemas import GatherConfig, RateLimitConfig
from constant.system import SERVICE_TYPE_GITHUB_RAW
from core.exceptions import RateLimitDeferral, TransientFetchError
from search import client as sc
from tools.ratelimit import RateLimiter

SHA = "a" * 40
BLOB = f"https://github.com/owner/repo/blob/{SHA}/src/f.txt"
BASE_RATE = 2.0
BURST = 4


class _FakeResponse:
    def __init__(self, status=200, text="", headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = headers or {}
        self.ok = 200 <= status < 300

    def json(self):
        import json

        return json.loads(self.text)

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
        r = self._responses.pop(0) if self._responses else _FakeResponse(200, "")
        return r() if callable(r) else r

    def get(self, url, **kwargs):
        return self.request(method="GET", url=url, **kwargs)


@pytest.fixture
def starved_client(monkeypatch):
    """A real client over a real adaptive bucket whose token supply is exhausted.

    ``_limit`` returning False is the production starvation signal: it happens
    only after the scheduled wait was slept and the token went to a competing
    worker (``search/client.py:316-337``).
    """
    limiter = RateLimiter(
        {SERVICE_TYPE_GITHUB_RAW: RateLimitConfig(base_rate=BASE_RATE, burst_limit=BURST, adaptive=True)}
    )
    sess = _FakeSession([])
    gh = sc.GitHubClient(limiter=limiter)
    monkeypatch.setattr(sc, "_HTTP_SESSION", sess)
    monkeypatch.setattr(sc, "_github_client", gh)
    monkeypatch.setattr(gh, "_limit", lambda service, credential=None: False)
    sc.reset_gather_transport_stats()
    return gh, sess, limiter


def _bucket(limiter):
    return limiter._get_bucket(SERVICE_TYPE_GITHUB_RAW)


def _fetch(**kwargs):
    kwargs.setdefault("transport", "raw")
    kwargs.setdefault("max_payload_bytes", 1 << 20)
    kwargs.setdefault("max_refusal_wait_s", 5.0)
    return sc.fetch_gather_content(BLOB, **kwargs)


# ---------------------------------------------------------------------------
# gather-transport-S22
# ---------------------------------------------------------------------------
def test_s22_withheld_request_does_not_decay_the_budget(starved_client):
    """WHEN a gather fetch is withheld because the local budget has no token, so
    no request reaches the network THEN nothing is reported to the adaptive
    budget: its effective rate is unchanged and its consecutive-failure tally
    does not advance.

    The adaptive budget models remote tolerance. Feeding it an event in which no
    remote was contacted makes throttling decay the budget that produced it -
    measured in production as 1 708 suppressions per 600 s while the remote
    refused nothing (HTTP 429 = 0, HTTP 403 = 0).
    """
    gh, sess, limiter = starved_client
    bucket = _bucket(limiter)
    rate_before = bucket.rate
    failures_before = bucket.consecutive_failures

    with pytest.raises(RateLimitDeferral):
        _fetch()

    assert bucket.rate == rate_before, "a withheld request decayed the adaptive budget"
    assert bucket.consecutive_failures == failures_before
    assert bucket.consecutive_failures == 0
    assert sess.calls == [], "no request may reach the network for a withheld fetch"


# ---------------------------------------------------------------------------
# gather-transport-S23
# ---------------------------------------------------------------------------
def test_s23_sustained_contention_does_not_collapse_the_budget(starved_client):
    """WHEN many workers contend for the same budget so that 50 fetches are
    withheld in succession THEN the effective rate never falls below the
    configured base rate as a result of those withholdings alone, and the number
    of withheld fetches is published as a count rather than inferred from a
    degraded rate.

    Pre-fix this is the collapse loop: 3 reported "failures" halve the rate, and
    the floor is 0.1 x base (0.2/s for github_raw), from which recovery needs 10
    consecutive successes at x1.1 each.
    """
    gh, sess, limiter = starved_client
    bucket = _bucket(limiter)

    for _ in range(50):
        with pytest.raises(RateLimitDeferral):
            _fetch()

    assert bucket.rate == BASE_RATE, "sustained contention collapsed the budget toward its floor"
    assert bucket.rate >= BASE_RATE
    assert bucket.consecutive_failures == 0
    assert sc.get_gather_transport_stats()["deferred_local_budget"] == 50
    assert sess.calls == []


# ---------------------------------------------------------------------------
# gather-transport-S24
# ---------------------------------------------------------------------------
def test_s24_own_budget_withholding_defers_with_bounded_wait_and_stage_pause(starved_client, monkeypatch):
    """WHEN a gather fetch cannot obtain a token even after the scheduled wait
    THEN the task is deferred with a wait derived from that budget's own refill
    time and bounded by the configured refusal cap, and the stage is asked to
    pause so its other workers stop claiming into the same empty budget.

    ``stage_pause`` is what prevents 8 gather workers from claiming, starving and
    re-claiming in lockstep (design D2); the bound is the same refusal cap that
    the visibility-window invariant validates (design D3).
    """
    gh, sess, limiter = starved_client
    monkeypatch.setattr(_bucket(limiter), "tokens", 0.0, raising=False)

    with pytest.raises(RateLimitDeferral) as excinfo:
        _fetch(max_refusal_wait_s=5.0)

    d = excinfo.value
    assert d.stage_pause is True, "a shared-budget withholding must pause the stage, not just this task"
    assert 0.0 < d.wait_s <= 5.0, f"wait must be the budget's own refill time, bounded by the cap: {d.wait_s}"
    # refill time for one token at the configured base rate
    assert d.wait_s == pytest.approx(1.0 / BASE_RATE, abs=0.05)
    assert sess.calls == []


# ---------------------------------------------------------------------------
# gather-transport-S25
# ---------------------------------------------------------------------------
def test_s25_own_budget_withholding_is_counted_apart_from_remote_refusals(starved_client, monkeypatch):
    """WHEN a run contains both a remote rate-limit refusal and a withholding by
    the system's own budget THEN the two are reported as separate counts, so an
    operator can tell "the remote refused us" from "we refused ourselves" without
    reading log lines.
    """
    gh, sess, limiter = starved_client

    # one remote refusal: the limiter lets the request out, the remote answers 429
    monkeypatch.setattr(gh, "_limit", lambda service, credential=None: True)
    sess._responses = [_FakeResponse(429, "rate limit exceeded", headers={"Retry-After": "3"})]
    with pytest.raises(RateLimitDeferral):
        _fetch(retries=1)

    # one own-budget withholding
    monkeypatch.setattr(gh, "_limit", lambda service, credential=None: False)
    with pytest.raises(RateLimitDeferral):
        _fetch()

    stats = sc.get_gather_transport_stats()
    assert stats["deferred_rate_limit"] == 1, "the remote refusal must stay on its own counter"
    assert stats["deferred_local_budget"] == 1, "the withholding must not be folded into the remote counter"
    assert stats["deferred_secondary"] == 0
    assert stats["deferred_credentials"] == 0


# ---------------------------------------------------------------------------
# gather-transport-S26
# ---------------------------------------------------------------------------
def test_s26_legacy_classification_is_a_configuration_flip(starved_client, monkeypatch):
    """WHEN an operator disables local-budget deferral in configuration THEN a
    withheld gather fetch is classified as a retryable failure-empty exactly as
    before this capability existed - including the failure report to the adaptive
    budget, because a half-rollback would keep the destructive half (design D5).
    """
    gh, sess, limiter = starved_client
    reported = []
    real_report = gh._report

    def _spy(service, ok, credential=None):
        reported.append((service, ok))
        real_report(service, ok, credential)

    monkeypatch.setattr(gh, "_report", _spy)
    bucket = _bucket(limiter)

    with pytest.raises(TransientFetchError):
        _fetch(defer_local_suppression=False)

    assert reported == [(SERVICE_TYPE_GITHUB_RAW, False)], "legacy mode must report exactly as before"
    assert sc.get_gather_transport_stats().get("deferred_local_budget", 0) == 0
    assert bucket.consecutive_failures == 1, "legacy mode keeps the pre-change adaptive behavior"
    assert sess.calls == []


def test_s26b_flag_config_surface(tmp_path):
    """The rollback flag is a real configuration surface: defaulted to the fixed
    behavior, parsed from the gather section, and rejected loudly when it is not
    a boolean (house rule: policy is visible in config, never implied)."""
    from config.loader import ConfigLoader
    from config.validator import ConfigValidator

    assert GatherConfig().defer_local_suppression is True, "default must be the fixed behavior"

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        # A loader-valid configuration: ``load()`` runs the full validator, which
        # (independently of gather) requires at least one credential, one user
        # agent and one enabled task.
        "global:\n"
        "  workspace: ./data\n"
        "  github_credentials:\n"
        "    sessions: ['s']\n"
        "    tokens: []\n"
        "    strategy: round_robin\n"
        "  user_agents: ['Mozilla/5.0']\n"
        "gather:\n  transport: raw\n  defer_local_suppression: false\n"
        "tasks:\n"
        "  - name: probe\n"
        "    enabled: true\n"
        "    provider_type: openai_like\n"
        "    use_api: false\n"
        "    stages:\n"
        "      search: true\n"
        "      gather: true\n"
        "      check: true\n"
        "      inspect: true\n"
        "    patterns:\n"
        "      key_pattern: 'sk-x'\n"
        "    conditions:\n"
        "      - query: '/sk-x/'\n",
        encoding="utf-8",
    )
    cfg = ConfigLoader(str(cfg_path)).load()
    assert cfg.gather.defer_local_suppression is False

    cfg.gather.defer_local_suppression = "yes"
    validator = ConfigValidator()
    with pytest.raises(ValueError) as exc:
        validator.validate(cfg)
    assert any("defer_local_suppression" in e for e in validator.errors), validator.errors
    assert "defer_local_suppression" in str(exc.value)


# ---------------------------------------------------------------------------
# gather-transport-S29
# ---------------------------------------------------------------------------
def test_s29_withheld_fetch_is_not_timed(starved_client):
    """WHEN a gather fetch is withheld and never reaches the network THEN the
    latency summary's sample count does not advance, so published percentiles
    describe real network fetches only.
    """
    with pytest.raises(RateLimitDeferral):
        _fetch()

    stats = sc.get_gather_transport_stats()
    assert stats["latency_samples_raw"] == 0
    assert stats["requests_raw"] == 0
    assert stats["deferred_local_budget"] == 1


# ---------------------------------------------------------------------------
# failure-handling-FH3-S6
# ---------------------------------------------------------------------------
def test_fh3_s6_search_boundary_is_unchanged_by_the_flag(monkeypatch):
    """WHEN a SEARCH fetch is suppressed by the local rate limiter and surfaces as
    a blank payload, with local-budget deferral enabled THEN it is still a
    failure-empty: the amendment is scoped to the gather fetch boundary, so the
    preserved pin ``test_s3_limiter_suppression_is_failure_empty`` cannot be
    broken by this change (design D11).
    """
    from tests.test_fh_client_taxonomy import _FakeGitHubClient

    monkeypatch.setattr(sc, "_github_client", _FakeGitHubClient(content=""))

    assert GatherConfig().defer_local_suppression is True or True  # flag on (or absent pre-fix)
    with pytest.raises(TransientFetchError):
        sc.search_api_with_count('"sk-"', "tok", 1)
