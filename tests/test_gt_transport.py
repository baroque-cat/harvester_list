"""Client-layer gather transport contract (gather-transport S4-S7, S9-S16, S19, S20).

RED at plan time: ``derive_raw_url`` / ``gather_target_url`` / ``fetch_gather_content``
/ ``get_gather_transport_stats`` do not exist in ``search/client.py``,
``SERVICE_TYPE_GITHUB_RAW`` does not exist in ``constant/system.py`` and
``RateLimitDeferral`` does not exist in ``core/exceptions.py`` - the import
failure IS the expected RED state (design D2/D3/D4/D6).

Network is stubbed at the single low-level HTTP seam (``_HTTP_SESSION``, which
``search.client.request`` at :208-212 delegates to), so throttling attribution,
classification and truncation logic all run for real while the suite stays
offline (design D11).
"""

import re

import pytest

from constant.system import (
    SERVICE_TYPE_GITHUB_API,
    SERVICE_TYPE_GITHUB_RAW,
    SERVICE_TYPE_GITHUB_WEB,
)
from core.exceptions import RateLimitDeferral, TransientFetchError
from search import client as sc

SHA = "a" * 40
BLOB = f"https://github.com/owner/repo/blob/{SHA}/src/f.txt"

# Captured live 2026-09-25 from GET https://api.github.com/search/code (token auth):
# item['sha'] is a BLOB hash, item['url'] carries ?ref=<commit sha>. Using the blob
# hash as a raw ref was measured to return HTTP 404.
CAPTURED_BLOB_SHA = "32cce148466eaa5a31334ec2b479ace5b7c32914"
CAPTURED_REV_SHA = "2095a3bf8af499e74caf5ccdb7ae783f5dec1afd"


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
    """Records every request and replays queued responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method=None, url=None, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if self._responses:
            r = self._responses.pop(0)
        else:
            r = _FakeResponse(200, "")
        return r() if callable(r) else r

    def get(self, url, **kwargs):
        return self.request(method="GET", url=url, **kwargs)


@pytest.fixture
def stub_http(monkeypatch):
    """Patch the session seam; returns a factory installing canned responses."""

    def install(*responses):
        sess = _FakeSession(list(responses))
        monkeypatch.setattr(sc, "_HTTP_SESSION", sess)
        monkeypatch.setattr(sc, "_github_client", None, raising=False)
        sc.reset_gather_transport_stats()
        return sess

    return install


@pytest.fixture
def real_client(monkeypatch):
    """A real GitHubClient over a stubbed session, so budgets are exercised."""
    from tools.ratelimit import RateLimiter

    sess = _FakeSession([])
    monkeypatch.setattr(sc, "_HTTP_SESSION", sess)
    limiter = RateLimiter({})
    gh = sc.GitHubClient(limiter=limiter)
    monkeypatch.setattr(sc, "_github_client", gh)
    sc.reset_gather_transport_stats()
    return gh, sess, limiter


# ---------------------------------------------------------------------------
# gather-transport-S4
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "discovered",
    [
        BLOB,
        BLOB + "?plain=1",
        BLOB + "#L12",
        BLOB + "?plain=1#L1-L40",
        "https://github.com/owner/repo/blob/" + SHA + "/docs/a%20b/c.md",
    ],
)
def test_s4_immutable_revision_is_used_verbatim(discovered):
    before = sc.get_gather_transport_stats().get("head_fallback", 0)

    got = sc.derive_raw_url(discovered)

    assert got is not None
    assert got.startswith("https://raw.githubusercontent.com/owner/repo/")
    # exact revision preserved verbatim, query and fragment dropped
    assert f"/{SHA}/" in got
    assert "?" not in got and "#" not in got
    assert got.endswith("/src/f.txt") or got.endswith("/docs/a%20b/c.md") or "c.md" in got
    assert sc.get_gather_transport_stats()["head_fallback"] == before


# ---------------------------------------------------------------------------
# gather-transport-S5
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "revision", ["main", "master", "HEAD~3", "a" * 39, "A" * 40, "feature/x", "v1.2.3"]
)
def test_s5_unusable_revision_falls_back_loudly(revision, caplog):
    url = f"https://github.com/owner/repo/blob/{revision}/src/f.txt"
    sc.reset_gather_transport_stats()

    with caplog.at_level("WARNING"):
        got = sc.derive_raw_url(url)

    assert got == f"https://raw.githubusercontent.com/owner/repo/HEAD/src/f.txt"
    assert sc.get_gather_transport_stats()["head_fallback"] == 1
    # the warning names the affected link
    assert any(url in r.message or revision in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# gather-transport-S6
# ---------------------------------------------------------------------------
def test_s6_blob_content_hash_is_never_mistaken_for_a_revision(stub_http):
    """The trap measured live: item['sha'] is a blob hash and 404s as a raw ref."""
    blob_url = f"https://github.com/langchain-ai/docs/blob/{CAPTURED_REV_SHA}/src/guardrails.mdx"

    good = sc.derive_raw_url(blob_url)
    assert CAPTURED_REV_SHA in good

    # A discovery payload carrying both fields must resolve via the revision ref.
    item = {"sha": CAPTURED_BLOB_SHA, "path": "src/guardrails.mdx", "html_url": blob_url}
    from_ref = sc.derive_raw_url(item["html_url"])
    assert CAPTURED_REV_SHA in from_ref
    assert CAPTURED_BLOB_SHA not in from_ref

    # And using the blob hash in its place is demonstrably unresolvable: the
    # address differs from the working one and resolves as not-found.
    wrong = f"https://raw.githubusercontent.com/langchain-ai/docs/{CAPTURED_BLOB_SHA}/src/guardrails.mdx"
    assert wrong != from_ref
    sess = stub_http(_FakeResponse(404, "404: Not Found"))
    with pytest.raises(TransientFetchError):
        sc.fetch_gather_content(wrong, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5)
    assert len(sess.calls) == 1  # terminal after a single attempt
    assert sc.get_gather_transport_stats()["dropped_not_found"] == 1


# ---------------------------------------------------------------------------
# gather-transport-S7
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not a url",
        "https://example.com/a/b",
        "https://github.com/owner/repo/tree/" + SHA + "/src",
        "owner/repo/src/f.txt",
        "https://github.com/owner/repo",
        None,
    ],
)
def test_s7_unparseable_link_degrades_safely(bad, stub_http):
    sess = stub_http()
    sc.reset_gather_transport_stats()

    got = sc.derive_raw_url(bad)  # must not raise into the worker loop
    assert got is None
    assert sc.get_gather_transport_stats()["unparseable_link"] >= 1

    # fetching such a link stays inside the existing typed-failure family
    with pytest.raises(TransientFetchError):
        sc.fetch_gather_content(bad, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5)
    assert sess.calls == []  # no request was ever issued for an unusable address


# ---------------------------------------------------------------------------
# gather-transport-S9
# ---------------------------------------------------------------------------
def test_s9_plain_content_host_resolves_to_a_real_budget(real_client, monkeypatch):
    gh, sess, limiter = real_client

    # the host is recognized as its own resource class instead of falling through
    # to None (which today silently disables _limit entirely)
    assert gh._service("https://raw.githubusercontent.com/o/r/" + SHA + "/f.txt") == SERVICE_TYPE_GITHUB_RAW
    assert SERVICE_TYPE_GITHUB_RAW == "github_raw"

    sess._responses = [_FakeResponse(200, "APP_KEY=hello")]
    limited, reported = [], []
    monkeypatch.setattr(gh, "_limit", lambda svc, cred=None: limited.append((svc, cred)) or True)
    monkeypatch.setattr(gh, "_report", lambda svc, ok, cred=None: reported.append((svc, ok)))

    out = sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5)

    assert out == "APP_KEY=hello"
    assert limited == [(SERVICE_TYPE_GITHUB_RAW, None)]  # token acquired BEFORE sending
    assert reported == [(SERVICE_TYPE_GITHUB_RAW, True)]  # outcome reported back
    assert len(sess.calls) == 1
    stats = sc.get_gather_transport_stats()
    assert stats["requests_raw"] == 1 and stats["bytes_raw"] == len("APP_KEY=hello")


def test_s9_failure_is_reported_to_the_adaptive_budget(real_client, monkeypatch):
    gh, sess, limiter = real_client
    sess._responses = [_FakeResponse(503, "unavailable"), _FakeResponse(503, "unavailable"), _FakeResponse(503, "x")]
    reported = []
    monkeypatch.setattr(gh, "_limit", lambda svc, cred=None: True)
    monkeypatch.setattr(gh, "_report", lambda svc, ok, cred=None: reported.append(ok))

    with pytest.raises(TransientFetchError):
        sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5, retries=1)
    assert reported and not any(reported)  # every attempt reported False


# ---------------------------------------------------------------------------
# gather-transport-S10
# ---------------------------------------------------------------------------
def test_s10_gathering_does_not_consume_the_search_budget(real_client, monkeypatch):
    gh, sess, limiter = real_client
    attributed = []
    monkeypatch.setattr(gh, "_limit", lambda svc, cred=None: attributed.append(svc) or True)
    monkeypatch.setattr(gh, "_report", lambda svc, ok, cred=None: None)
    sess._responses = [_FakeResponse(200, "x" * 10) for _ in range(5)]

    for _ in range(5):
        sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5)

    assert attributed.count(SERVICE_TYPE_GITHUB_RAW) == 5
    assert SERVICE_TYPE_GITHUB_API not in attributed  # the 10/min code-search budget is untouched
    assert sc.get_gather_transport_stats()["requests_raw"] == 5


# ---------------------------------------------------------------------------
# gather-transport-S11
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "transport, expected_service, expected_host",
    [
        ("html", SERVICE_TYPE_GITHUB_WEB, "github.com"),
        ("rest", SERVICE_TYPE_GITHUB_API, "api.github.com"),
    ],
)
def test_s11_authenticated_transports_keep_their_existing_budgets(
    real_client, monkeypatch, transport, expected_service, expected_host
):
    gh, sess, limiter = real_client
    attributed = []
    monkeypatch.setattr(gh, "_limit", lambda svc, cred=None: attributed.append(svc) or True)
    monkeypatch.setattr(gh, "_report", lambda svc, ok, cred=None: None)
    body = '{"content":"aGk=","encoding":"base64"}' if transport == "rest" else "<html>hi</html>"
    sess._responses = [_FakeResponse(200, body)]

    sc.fetch_gather_content(BLOB, transport=transport, max_payload_bytes=1 << 20, max_refusal_wait_s=5)

    assert attributed == [expected_service]
    assert expected_host in sess.calls[0]["url"]
    assert sc.get_gather_transport_stats()[f"requests_{transport}"] == 1


def test_s11_existing_authenticated_caller_sequence_is_unchanged(real_client, monkeypatch):
    """repo_meta's path through get_with_status must keep its exact call sequence."""
    gh, sess, limiter = real_client
    order = []
    monkeypatch.setattr(gh, "_limit", lambda svc, cred=None: order.append("limit") or True)
    monkeypatch.setattr(gh, "_report", lambda svc, ok, cred=None: order.append("report"))
    monkeypatch.setattr(gh, "mark_success", lambda *a, **k: order.append("mark_success"), raising=False)
    sess._responses = [_FakeResponse(200, '{"name":"r"}')]

    status, body = gh.get_with_status("https://api.github.com/repos/o/r")[:2]
    assert status == 200
    assert order[:2] == ["limit", "report"]  # acquire before send, report after


# ---------------------------------------------------------------------------
# gather-transport-S12
# ---------------------------------------------------------------------------
def test_s12_forbidden_with_rate_limit_marker_is_a_deferral(stub_http, caplog):
    body = '{"message":"You have exceeded a secondary rate limit. Please wait a few minutes."}'
    sess = stub_http(_FakeResponse(403, body, {"Retry-After": "120"}))

    with caplog.at_level("ERROR"):
        with pytest.raises(RateLimitDeferral) as exc:
            sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=60)

    stats = sc.get_gather_transport_stats()
    assert stats["deferred_secondary"] == 1
    assert stats["dropped_auth"] == 0  # NOT classified as authentication failure
    assert "Authentication failed" not in caplog.text
    # a deferral is not abandonment after a single attempt: no drop counter moved
    assert stats["dropped_not_found"] == 0
    assert exc.value.wait_s <= 60  # bounded by the configured cap


# ---------------------------------------------------------------------------
# gather-transport-S13
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "status, body",
    [(401, '{"message":"Bad credentials"}'), (403, '{"message":"Resource not accessible by integration"}')],
)
def test_s13_genuine_authentication_failure_drops_loudly_once(stub_http, caplog, status, body):
    sess = stub_http(*[_FakeResponse(status, body)] * 6)

    with caplog.at_level("WARNING"):
        with pytest.raises(TransientFetchError) as exc:
            sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5, retries=3)

    assert not isinstance(exc.value, RateLimitDeferral)
    assert len(sess.calls) == 1  # exactly one attempt, no retry storm
    stats = sc.get_gather_transport_stats()
    assert stats["dropped_auth"] == 1
    assert stats["deferred_rate_limit"] == 0 and stats["deferred_secondary"] == 0
    assert caplog.records  # loud


# ---------------------------------------------------------------------------
# gather-transport-S14
# ---------------------------------------------------------------------------
def test_s14_missing_resource_drops_loudly_once(stub_http, caplog):
    sess = stub_http(*[_FakeResponse(404, "404: Not Found")] * 6)

    with caplog.at_level("WARNING"):
        with pytest.raises(TransientFetchError):
            sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=5, retries=3)

    assert len(sess.calls) == 1
    stats = sc.get_gather_transport_stats()
    assert stats["dropped_not_found"] == 1
    assert stats["deferred_rate_limit"] == 0 and stats["dropped_auth"] == 0


# ---------------------------------------------------------------------------
# gather-transport-S15
# ---------------------------------------------------------------------------
def test_s15_finite_quota_exhaustion_defers_without_burning_attempts(stub_http):
    sess = stub_http(_FakeResponse(429, '{"message":"API rate limit exceeded"}', {"Retry-After": "7"}))

    with pytest.raises(RateLimitDeferral) as exc:
        sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=60)

    assert exc.value.wait_s == 7  # the published finite wait is honored
    stats = sc.get_gather_transport_stats()
    assert stats["deferred_rate_limit"] == 1
    assert stats["deferred_secondary"] == 0
    # deferral is not a task fault: nothing was dropped
    assert stats["dropped_auth"] == 0 and stats["dropped_not_found"] == 0


def test_s15_signalled_wait_above_the_cap_is_clamped(stub_http):
    stub_http(_FakeResponse(429, '{"message":"rate limit"}', {"Retry-After": "900"}))

    with pytest.raises(RateLimitDeferral) as exc:
        sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=60)

    assert exc.value.wait_s == 60  # runtime clamp of design D5: WAIT_CAP bounds every sleep


# ---------------------------------------------------------------------------
# gather-transport-S16
# ---------------------------------------------------------------------------
def test_s16_actor_scoped_abuse_refusal_pauses_the_stage(stub_http, caplog):
    """No Retry-After and no X-RateLimit-Reset => actor-scoped abuse detection."""
    body = "<html><body>Your account has been flagged for abuse detection activity.</body></html>"
    sess = stub_http(*[_FakeResponse(403, body)] * 4)

    with caplog.at_level("ERROR"):
        with pytest.raises(RateLimitDeferral) as exc:
            sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=45)

    assert len(sess.calls) == 1  # stops generating traffic immediately
    assert exc.value.wait_s <= 45  # the stage pause is bounded
    assert exc.value.stage_pause is True  # escalates wider than a per-token quota wait
    stats = sc.get_gather_transport_stats()
    assert stats["deferred_secondary"] == 1
    assert any(r.levelname == "ERROR" for r in caplog.records)  # loud emergency


# ---------------------------------------------------------------------------
# gather-transport-S19
# ---------------------------------------------------------------------------
def test_s19_plainer_payload_loses_no_findings():
    """Same real file, same immutable revision, both transports, captured 2026-09-25.

    Fixtures are sanitized: every secret-shaped token was replaced by a
    deterministic sha256-derived synthetic of identical shape, preserving match
    count and distinctness (see each fixture's PROVENANCE header).
    """
    from pathlib import Path

    fix = Path(__file__).parent / "fixtures"
    raw = (fix / "live_2026_09_gt_raw_payload.txt").read_text(encoding="utf-8")
    html = (fix / "live_2026_09_gt_blob_embedded.html").read_text(encoding="utf-8")
    pattern = r"AKIA[0-9A-Z]{16}"

    found_raw = set(sc.extract(raw, pattern))
    found_html = set(sc.extract(html, pattern))

    assert found_raw, "fixture pair must be non-vacuous: the captured file carries a token"
    assert found_raw >= found_html, "plain-content extraction must be a superset of rendered-page extraction"


# ---------------------------------------------------------------------------
# gather-transport-S20
# ---------------------------------------------------------------------------
def test_s20_oversized_payload_is_truncated_and_still_searched(stub_http, caplog):
    inside = "AKIA" + "A" * 12 + "INSIDE"
    beyond = "AKIA" + "B" * 12 + "BEYOND"
    filler = "." * 200
    payload = f"{inside}\n{filler}\n{beyond}\n"
    stub_http(_FakeResponse(200, payload))

    cap = 64
    with caplog.at_level("WARNING"):
        text = sc.fetch_gather_content(BLOB, transport="raw", max_payload_bytes=cap, max_refusal_wait_s=5)

    assert len(text) <= cap  # reading stopped at the cap
    stats = sc.get_gather_transport_stats()
    assert stats["truncated"] == 1
    # retained prefix is still searched, not discarded
    found = set(sc.extract(text, r"AKIA[0-9A-Z]{16}"))
    assert inside[:20] in found
    assert beyond[:20] not in found
    assert stats["requests_raw"] == 1  # not treated as a fetch failure


# ---------------------------------------------------------------------------
# tasks.md 3.6 (verification W3): transport=None resolves against configuration
# ---------------------------------------------------------------------------
def _load_global_config(monkeypatch, transport):
    """Install a process-wide Config so ``config.get_config()`` succeeds."""
    import config as config_pkg
    from config.schemas import Config

    cfg = Config()
    cfg.gather.transport = transport
    monkeypatch.setattr(config_pkg, "_config_instance", cfg, raising=False)
    return cfg


def test_collect_without_explicit_transport_honours_configuration(stub_http, monkeypatch):
    """GIVEN an operator who flipped ``gather.transport`` to the rollback value
    WHEN ``collect()`` is called on the URL path without an explicit transport
    THEN the configured surface is used - not a hardcoded default.

    RED before the fix: ``collect()`` resolved ``None`` to a literal ``"raw"``,
    so any embedded or future caller silently ignored the operator's rollback
    flag and broke design D2 ("rollback is a flag flip and nothing else").
    """
    payload = "AKIA" + "A" * 16 + "\n"

    # 1. No configuration loaded at all -> built-in default, and it must not raise.
    import config as config_pkg

    monkeypatch.setattr(config_pkg, "_config_instance", None, raising=False)
    assert sc._configured_gather_transport() == "raw"

    sess = stub_http(_FakeResponse(200, payload))
    sc.collect(key_pattern=r"AKIA[0-9A-Z]{16}", url=BLOB)
    assert sess.calls[0]["url"].startswith("https://raw.githubusercontent.com/")

    # 2. Configuration says html (the rollback value) -> the rendered host is used.
    sc.reset_gather_transport_stats()
    _load_global_config(monkeypatch, "html")
    assert sc._configured_gather_transport() == "html"

    sess = stub_http(_FakeResponse(200, payload))
    sc.collect(key_pattern=r"AKIA[0-9A-Z]{16}", url=BLOB)
    assert sess.calls[0]["url"] == BLOB
    assert sc.get_gather_transport_stats()["requests_html"] == 1
    assert sc.get_gather_transport_stats()["requests_raw"] == 0

    # 3. An explicit argument always wins over configuration.
    sc.reset_gather_transport_stats()
    sess = stub_http(_FakeResponse(200, payload))
    sc.collect(key_pattern=r"AKIA[0-9A-Z]{16}", url=BLOB, transport="raw")
    assert sess.calls[0]["url"].startswith("https://raw.githubusercontent.com/")
    assert sc.get_gather_transport_stats()["requests_raw"] == 1


def test_configured_transport_lookup_degrades_instead_of_raising(monkeypatch):
    """The resolver must never turn a missing/broken configuration into a hard
    failure inside the client layer (design D6: no new coupling, fail-open)."""
    import config as config_pkg

    monkeypatch.setattr(config_pkg, "_config_instance", None, raising=False)
    assert sc._configured_gather_transport() == sc._DEFAULT_GATHER_TRANSPORT == "raw"

    class _Broken:
        def gather(self):  # pragma: no cover - attribute access must not explode
            raise RuntimeError("boom")

    monkeypatch.setattr(config_pkg, "_config_instance", _Broken(), raising=False)
    assert sc._configured_gather_transport() == "raw"
