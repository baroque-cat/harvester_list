"""Estimate-unavailable sentinel (slice 6.4 П-K′, wave 6).

Traceability: 02_critical_fixes.md:100 (П-K′), failure-handling spec.

RED at plan time: ``EstimateUnavailable`` does not exist in core.exceptions;
``estimate_web_total`` returns ``WEB_RESULTS_PER_PAGE`` (20) on any failure,
causing the stage's pagination/refine decisions to collapse to page-1-only
(``total > per_page`` is False when total=20 and per_page=20).

No network, no real sleeping: blackbird delay is monkeypatched.
HTTP layer is mocked at the minimal point (``_github_client`` or
``search_github_web``) so the real classification/parse logic executes.
"""

import json

import pytest

from core.exceptions import EstimateUnavailable
from search import client as search_client
from tools.state import GithubCredentialLimited


# ---------------------------------------------------------------------------
# PK1: blackbird HTTPError/exception ⇒ EstimateUnavailable (NOT 20)
# ---------------------------------------------------------------------------
def test_pk1_blackbird_exception_raises_estimate_unavailable(monkeypatch):
    """PK1: WHEN blackbird_count raises an exception THEN EstimateUnavailable,
    NOT a small numeric fallback (20).

    Mocks search_github_web (returns valid content) and _github_client.get
    (raises on blackbird URL) so estimate_web_total executes fully.
    """
    # search_github_web returns valid content
    monkeypatch.setattr(
        search_client, "search_github_web",
        lambda query, session, page: "<html><body>results</body></html>"
    )
    # Mock get_github_client to return a client that raises on blackbird
    real_client = search_client.get_github_client()
    original_get = real_client.get

    def mock_get(url, **kwargs):
        if "blackbird_count" in url:
            raise TimeoutError("blackbird timeout")
        return original_get(url, **kwargs)

    monkeypatch.setattr(real_client, "get", mock_get)
    # Mock sleep to avoid delay
    monkeypatch.setattr(search_client.time, "sleep", lambda x: None)
    # Reset stats
    search_client.reset_estimate_unavailable_stats()

    with pytest.raises(EstimateUnavailable):
        search_client.estimate_web_total("/sk-abc/", "session123")

    # Counter incremented
    stats = search_client.get_estimate_unavailable_stats()
    assert stats["estimate_unavailable"] >= 1


# ---------------------------------------------------------------------------
# PK2: initial content blank ⇒ EstimateUnavailable (NOT 20)
# ---------------------------------------------------------------------------
def test_pk2_initial_content_blank_raises_estimate_unavailable(monkeypatch):
    """PK2: WHEN initial search returns blank content THEN EstimateUnavailable,
    NOT a small numeric fallback (20).

    Mocks search_github_web to return "" (blank) so estimate_web_total
    executes the blank-content branch.
    """
    monkeypatch.setattr(search_client, "search_github_web", lambda q, s, p: "")
    search_client.reset_estimate_unavailable_stats()

    with pytest.raises(EstimateUnavailable):
        search_client.estimate_web_total("/sk-abc/", "session123")

    stats = search_client.get_estimate_unavailable_stats()
    assert stats["estimate_unavailable"] >= 1


# ---------------------------------------------------------------------------
# PK3: extract_count_from_page blank ⇒ EstimateUnavailable (NOT 20)
# ---------------------------------------------------------------------------
def test_pk3_extract_count_blank_raises_estimate_unavailable():
    """PK3: WHEN extract_count_from_page receives blank content THEN
    EstimateUnavailable, NOT a small numeric fallback (20).
    """
    search_client.reset_estimate_unavailable_stats()

    with pytest.raises(EstimateUnavailable):
        search_client.extract_count_from_page("", "/sk-abc/")

    stats = search_client.get_estimate_unavailable_stats()
    assert stats["estimate_unavailable"] >= 1


# ---------------------------------------------------------------------------
# PK4 (MAIN): stage decision does NOT collapse to page-1-only
# ---------------------------------------------------------------------------
def test_pk4_stage_pagates_anyway_on_estimate_unavailable(monkeypatch):
    """PK4: WHEN estimate_web_total raises EstimateUnavailable THEN
    search_web_with_count returns total=WEB_LIMIT (100) so the stage
    paginates (total > per_page) but does NOT refine (total > limit is False).

    This is the MAIN pin: the stage decision must not collapse to page-1-only.
    """
    from constant.search import WEB_LIMIT, WEB_RESULTS_PER_PAGE

    # Mock search_github_web to return valid content with results
    content = '<a href="/o/r/blob/main/a.py#L10">link</a>'
    monkeypatch.setattr(search_client, "search_github_web", lambda q, s, p: content)
    
    # Mock estimate_web_total to raise EstimateUnavailable
    def raise_unavailable(q, s, c=None):
        raise EstimateUnavailable("test")
    
    monkeypatch.setattr(search_client, "estimate_web_total", raise_unavailable)
    # Mock sleep to avoid delay
    monkeypatch.setattr(search_client.time, "sleep", lambda x: None)

    results, total, returned_content = search_client.search_web_with_count(
        "/sk-abc/", "session123", page=1
    )

    # total must be WEB_LIMIT (100), NOT WEB_RESULTS_PER_PAGE (20)
    assert total == WEB_LIMIT, f"expected {WEB_LIMIT}, got {total}"
    # total > per_page must be True (pagination triggers)
    assert total > WEB_RESULTS_PER_PAGE, "total must trigger pagination"
    # total > limit must be False (refine does NOT trigger)
    assert not (total > WEB_LIMIT), "total must NOT trigger refine"
    # results and content preserved
    assert len(results) > 0
    assert returned_content == content


# ---------------------------------------------------------------------------
# PK5: healthy estimate ⇒ total==N, behavior byte-identical
# ---------------------------------------------------------------------------
def test_pk5_healthy_estimate_returns_count(monkeypatch):
    """PK5: WHEN blackbird_count returns {failed:false, count:N} THEN
    estimate_web_total returns N, behavior byte-identical to pre-fix.
    """
    # Mock search_github_web to return valid content (with keyword args)
    monkeypatch.setattr(
        search_client, "search_github_web",
        lambda query, session, page: "<html><body>results</body></html>"
    )
    # Mock get_user_agent to avoid config dependency
    monkeypatch.setattr(search_client, "get_user_agent", lambda: "test-agent")
    # Mock blackbird response
    blackbird_response = json.dumps({"failed": False, "count": 42, "mode": "COUNT_MODE_EXACT"})
    
    class FakeClient:
        def get(self, url, headers=None, interval=0, credential=None, **kwargs):
            return blackbird_response
    
    # Mock get_github_client at module level
    monkeypatch.setattr(search_client, "get_github_client", lambda: FakeClient())
    # Mock sleep to avoid delay
    monkeypatch.setattr(search_client.time, "sleep", lambda x: None)

    total = search_client.estimate_web_total("/sk-abc/", "session123")

    assert total == 42, f"expected 42, got {total}"


# ---------------------------------------------------------------------------
# PK6: API path not affected (with_api=True ⇒ estimate_web_total not called)
# ---------------------------------------------------------------------------
def test_pk6_api_path_not_affected(monkeypatch):
    """PK6: WHEN with_api=True THEN estimate_web_total is NOT called (API
    transport gets total from the API response, not from estimate_web_total).
    """
    # Spy on estimate_web_total
    called = []
    original = search_client.estimate_web_total

    def spy(*args, **kwargs):
        called.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(search_client, "estimate_web_total", spy)
    # Mock search_api_with_count to return a valid response
    monkeypatch.setattr(
        search_client, "search_api_with_count",
        lambda q, s, p, pp, metadata=None: (["url1"], 100, "{}")
    )

    results, total, content = search_client.search_with_count(
        "/sk-abc/", "token123", page=1, with_api=True, peer_page=100
    )

    # estimate_web_total must NOT be called for API path
    assert len(called) == 0, "estimate_web_total must not be called for API path"
    assert total == 100


# ---------------------------------------------------------------------------
# PK7 (composition with 6.1): GithubCredentialLimited propagates through
# ---------------------------------------------------------------------------
def test_pk7_credential_limited_propagates_through(monkeypatch):
    """PK7: WHEN GithubCredentialLimited is raised inside estimate_web_total
    (dead session detected) THEN it propagates through, NOT collapsed into
    EstimateUnavailable or a numeric fallback.
    """
    # Mock search_github_web to return valid content (with keyword args)
    monkeypatch.setattr(
        search_client, "search_github_web",
        lambda query, session, page: "<html><body>results</body></html>"
    )
    # Mock get_user_agent to avoid config dependency
    monkeypatch.setattr(search_client, "get_user_agent", lambda: "test-agent")
    
    # Mock get_github_client to raise GithubCredentialLimited on blackbird
    class FakeClient:
        def get(self, url, headers=None, interval=0, credential=None, **kwargs):
            if "blackbird_count" in url:
                raise GithubCredentialLimited(
                    service="github_web", credential="dead_session",
                    wait=14400.0, reason="expired_session"
                )
            return ""
    
    monkeypatch.setattr(search_client, "get_github_client", lambda: FakeClient())
    # Mock sleep to avoid delay
    monkeypatch.setattr(search_client.time, "sleep", lambda x: None)

    with pytest.raises(GithubCredentialLimited) as exc_info:
        search_client.estimate_web_total("/sk-abc/", "dead_session")

    # Must be GithubCredentialLimited, NOT EstimateUnavailable
    assert exc_info.value.reason == "expired_session"


# ---------------------------------------------------------------------------
# PK8 (optional): counter/WARNING observability
# ---------------------------------------------------------------------------
def test_pk8_estimate_unavailable_counter_and_warning(monkeypatch):
    """PK8: WHEN estimate is unavailable THEN counter incremented and
    loud-once WARNING logged (first time only).
    """
    monkeypatch.setattr(search_client, "search_github_web", lambda q, s, p: "")
    search_client.reset_estimate_unavailable_stats()

    # First call: counter incremented, warning shown
    with pytest.raises(EstimateUnavailable):
        search_client.estimate_web_total("/sk-abc/", "session123")

    stats1 = search_client.get_estimate_unavailable_stats()
    assert stats1["estimate_unavailable"] == 1

    # Second call: counter incremented again
    with pytest.raises(EstimateUnavailable):
        search_client.estimate_web_total("/sk-def/", "session123")

    stats2 = search_client.get_estimate_unavailable_stats()
    assert stats2["estimate_unavailable"] == 2
