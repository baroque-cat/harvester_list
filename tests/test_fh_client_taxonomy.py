"""Client-boundary empty-result taxonomy (fix-silent-losses).

Traceability: failure-handling-S1, S2, S3.

Harness: the module-level GitHubClient singleton and ``search_github_web``
are monkeypatched; no network. Contract under test (design D1/D2):
``search_api_with_count`` / ``search_web_with_count`` raise a typed
``core.exceptions.TransientFetchError`` on any failure-empty (transport
exception, blank/suppressed payload) and return normally on legitimate
zeros (HTTP 200 parsed with zero items).

RED state note: ``TransientFetchError`` does not exist yet - the import
failure IS the expected RED for the whole file. Once the class exists,
S1 is a regression guard expected GREEN (legitimate-zero semantics are
preserved by the fix, not changed).
"""

import json

import pytest

from core.exceptions import TransientFetchError  # noqa: F401  (RED driver: absent pre-fix)
from search import client as search_client

ZERO_ITEMS_JSON = json.dumps({"total_count": 0, "incomplete_results": False, "items": []})


class _FakeGitHubClient:
    """Stand-in for the module singleton; mirrors get()/get_with_headers()."""

    def __init__(self, content="", raise_exc=None):
        self._content = content
        self._raise = raise_exc

    def get(self, url, headers=None, params=None, retries=3, interval=0, timeout=10, credential=None):
        if self._raise:
            raise self._raise
        return self._content

    def get_with_headers(self, *args, **kwargs):
        if self._raise:
            raise self._raise
        return self._content, {}


# ---------------------------------------------------------------------------
# failure-handling-S1
# ---------------------------------------------------------------------------
def test_s1_legitimate_zero_passes_through_cleanly(monkeypatch):
    """WHEN HTTP 200 answers with an empty item set THEN ([], 0, content) and no error."""
    monkeypatch.setattr(search_client, "_github_client", _FakeGitHubClient(content=ZERO_ITEMS_JSON))

    results, total, content = search_client.search_api_with_count('"sk-"', "tok", 1)

    assert results == []
    assert total == 0
    # The real answer body is preserved - not collapsed into a suppressed blank.
    assert content == ZERO_ITEMS_JSON


# ---------------------------------------------------------------------------
# failure-handling-S2
# ---------------------------------------------------------------------------
def test_s2_api_network_failure_is_transient_fetch_error(monkeypatch):
    """WHEN the API fetch exhausts retries on a network error THEN TransientFetchError."""
    monkeypatch.setattr(
        search_client,
        "_github_client",
        _FakeGitHubClient(raise_exc=TimeoutError("Request timeout: boom")),
    )

    with pytest.raises(TransientFetchError):
        search_client.search_api_with_count('"sk-"', "tok", 1)


def test_s2_web_blank_content_is_transient_fetch_error(monkeypatch):
    """WHEN web search yields blank content THEN TransientFetchError, never ([], 0, '')."""
    monkeypatch.setattr(search_client, "search_github_web", lambda q, s, p: "")

    with pytest.raises(TransientFetchError):
        search_client.search_web_with_count("/sk-x/", "sess", 1)


# ---------------------------------------------------------------------------
# failure-handling-S3
# ---------------------------------------------------------------------------
def test_s3_limiter_suppression_is_failure_empty(monkeypatch):
    """WHEN the local limiter suppresses the request (blank payload, no network) THEN failure-empty."""
    # get_with_headers() returns ("", {}) on _limit() suppression; get() then yields "".
    monkeypatch.setattr(search_client, "_github_client", _FakeGitHubClient(content=""))

    with pytest.raises(TransientFetchError):
        search_client.search_api_with_count('"sk-"', "tok", 1)


# ---------------------------------------------------------------------------
# failure-handling-S18 (fix-gather-transport: deferral is a third category)
# ---------------------------------------------------------------------------
def test_s18_rate_limit_refusal_is_a_deferral_not_an_answer(monkeypatch):
    """WHEN a gather fetch is refused on a published rate limit before any payload
    THEN RateLimitDeferral - not an empty answer, not a completed gather, and the
    counter is distinct from the failure-empty / drop accounting.

    RED at plan time: ``RateLimitDeferral`` does not exist in core.exceptions and
    ``fetch_gather_content`` does not exist in search.client (design D4/D6).
    Taxonomy pin: a deferral is NOT a TransientFetchError - no existing
    failure-empty handler may catch one and collapse it into a task fault.
    """
    from core.exceptions import RateLimitDeferral
    from tests.test_gt_transport import BLOB, _FakeResponse, _FakeSession

    sess = _FakeSession(
        [_FakeResponse(429, '{"message":"API rate limit exceeded"}', {"Retry-After": "5"})]
    )
    monkeypatch.setattr(search_client, "_HTTP_SESSION", sess)
    monkeypatch.setattr(search_client, "_github_client", None, raising=False)
    search_client.reset_gather_transport_stats()

    with pytest.raises(RateLimitDeferral) as exc:
        search_client.fetch_gather_content(
            BLOB, transport="raw", max_payload_bytes=1 << 20, max_refusal_wait_s=30
        )

    # A deferral is its own category: not a failure-empty typed transient failure.
    assert not isinstance(exc.value, TransientFetchError)
    stats = search_client.get_gather_transport_stats()
    assert stats["deferred_rate_limit"] == 1          # counted...
    assert stats["dropped_auth"] == 0                  # ...separately from drops...
    assert stats["dropped_not_found"] == 0
    assert len(sess.calls) == 1                        # refused before any payload
