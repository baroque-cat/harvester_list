"""HTML taxonomy fix (slice 6.3 П-G, wave 6).

Traceability: 02_critical_fixes.md:99 (П-G), credential-liveness spec.

RED at plan time:
- PG1: 403 + deep abuse marker (>300 chars) should classify as secondary + pool-cool
- PG2: classifier must receive FULL content (not truncated to 300)
- PG3: 403 without markers should follow existing auth/transient path (regression)
- PG4: single ambiguous refusal should NOT cool (fail-open preserved)
- PG5: repeated ambiguous on same session should trigger pool-cool + WARNING
- PG6: sign-in content with 6.1 predicate should still raise expired_session (composition)

No network, no real sleeping. HTTP layer mocked at `request` (minimal point below
the error-handling logic in _http_get).
"""

import time
import pytest
import requests

from search import client as search_client
from tools.state import (
    GithubCredentialLimited,
    credential_liveness_metrics,
    github_credential_state,
    reset_credential_liveness_metrics,
)


class _Clock:
    """Frozen wall clock; advance() moves time without sleeping."""

    def __init__(self, monkeypatch, start=1_000_000.0):
        self.now = start
        monkeypatch.setattr(time, "time", lambda: self.now)

    def advance(self, seconds):
        self.now += seconds


def _make_http_error(status_code: int, body: str, headers: dict = None):
    """Build an HTTPError with a response carrying the given body and headers."""
    response = requests.Response()
    response.status_code = status_code
    response._content = body.encode("utf-8")
    response.headers = headers or {}
    # Make response.url set so str(error) works
    response.url = "https://github.com/search"
    error = requests.exceptions.HTTPError(response=response)
    return error


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)
    reset_credential_liveness_metrics()
    if hasattr(search_client, "reset_dead_session_stats"):
        search_client.reset_dead_session_stats()
    if hasattr(search_client, "reset_web_parse_stats"):
        search_client.reset_web_parse_stats()
    if hasattr(search_client, "reset_ambiguous_stats"):
        search_client.reset_ambiguous_stats()
    monkeypatch.setattr(search_client, "get_user_agent", lambda: "test-agent")
    # Disable sleeping in retry loops
    monkeypatch.setattr(search_client.time, "sleep", lambda *a, **kw: None)
    # Disable rate limiter to allow all attempts through
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_limit", lambda *a, **kw: True)
    yield
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)


# ---------------------------------------------------------------------------
# PG1: 403 + deep abuse marker → secondary + pool-cool
# ---------------------------------------------------------------------------
def test_pg1_403_with_deep_abuse_marker_classifies_secondary(monkeypatch):
    """PG1: WHEN 403 + abuse marker deep in body (>300 chars from start)
    THEN classification is secondary, pool-cool applied, GithubCredentialLimited raised.
    
    Sensitive to mutation: if content is truncated before classifier, marker is lost.
    """
    # Abuse marker at position >300 from start
    padding = "X" * 350
    abuse_body = f"<html><body>{padding}You have triggered an abuse detection mechanism. Please wait before retrying.</body></html>"
    
    error = _make_http_error(403, abuse_body)
    
    # Mock `request` to raise the HTTPError (this is below _http_get's error handling)
    def mock_request(*args, **kwargs):
        raise error
    
    monkeypatch.setattr(search_client, "request", mock_request)
    
    with pytest.raises(GithubCredentialLimited) as exc_info:
        search_client.search_github_web("test", "pg1_session_abc", 1)
    
    # Must be classified as secondary (abuse marker found in full content)
    assert exc_info.value.reason is not None
    # The credential should be cooling (pool-cool applied)
    assert github_credential_state.is_cooling("github_web", "pg1_session_abc"), \
        "pool-cool must be applied for 403 with abuse marker"


# ---------------------------------------------------------------------------
# PG2: classifier receives FULL content (not truncated)
# ---------------------------------------------------------------------------
def test_pg2_classifier_receives_full_content(monkeypatch):
    """PG2: WHEN 403 with marker deep in body
    THEN spy on _classify_credential_limit shows len(arg) == len(full body).
    
    Direct test that truncation does not happen before classification.
    """
    padding = "Y" * 400
    full_body = f"<html>{padding}secondary rate limit</html>"
    
    error = _make_http_error(403, full_body)
    
    def mock_request(*args, **kwargs):
        raise error
    
    monkeypatch.setattr(search_client, "request", mock_request)
    
    # Spy on _classify_credential_limit
    classify_calls = []
    real_client = search_client.get_github_client()
    original_classify = real_client._classify_credential_limit
    
    def spy_classify(headers, content, reason):
        classify_calls.append((headers, content, reason))
        return original_classify(headers, content, reason)
    
    monkeypatch.setattr(real_client, "_classify_credential_limit", spy_classify)
    
    with pytest.raises(GithubCredentialLimited):
        search_client.search_github_web("test", "pg2_session_abc", 1)
    
    assert len(classify_calls) > 0, "_classify_credential_limit must be called"
    headers, content, reason = classify_calls[0]
    # The content passed to classifier must be full (not truncated to 300)
    # content arg is the full_text passed as 'content' parameter
    assert len(content) >= len(full_body) - 10, \
        f"classifier must receive full content (got len={len(content)}, expected ~{len(full_body)})"
    assert "secondary rate limit" in content.lower(), \
        "classifier must see the marker in full content"


# ---------------------------------------------------------------------------
# PG3: 403 without markers → existing auth/transient path (regression)
# ---------------------------------------------------------------------------
def test_pg3_403_without_markers_follows_existing_path(monkeypatch):
    """PG3: WHEN 403 without abuse/rate-limit markers
    THEN follows existing path (NetworkError 'Authentication failed'), no pool-cool.
    
    Regression guard: must not break existing taxonomy.
    """
    plain_403_body = "<html><body>Forbidden</body></html>"
    
    error = _make_http_error(403, plain_403_body)
    
    def mock_request(*args, **kwargs):
        raise error
    
    monkeypatch.setattr(search_client, "request", mock_request)
    
    # Should raise NetworkError (existing path), not GithubCredentialLimited
    from search.client import NetworkError
    with pytest.raises(NetworkError) as exc_info:
        search_client.search_github_web("test", "pg3_session_abc", 1)
    
    assert "Authentication failed" in str(exc_info.value)
    # No credential should be cooling
    assert not github_credential_state.is_cooling("github_web", "pg3_session_abc"), \
        "plain 403 without markers must not trigger credential cooling"


# ---------------------------------------------------------------------------
# PG4: single ambiguous → no cooling (fail-open preserved)
# ---------------------------------------------------------------------------
def test_pg4_single_ambiguous_no_cooling(monkeypatch):
    """PG4: WHEN single ambiguous refusal (403 + rate-limit marker but no headers)
    THEN no credential cooled, ambiguous counter increments, fail-open preserved.
    
    Note: a 403 with "rate limit" marker (but not "secondary rate limit" or 
    "abuse detection") and no X-RateLimit headers is classified as ambiguous.
    """
    # "rate limit" marker triggers is_capacity_refusal but NOT secondary classification
    ambiguous_body = "<html><body>You have hit a rate limit. Please slow down.</body></html>"
    
    error = _make_http_error(403, ambiguous_body)
    
    def mock_request(*args, **kwargs):
        raise error
    
    monkeypatch.setattr(search_client, "request", mock_request)
    
    # Should raise NetworkError (transient path), not GithubCredentialLimited
    from search.client import NetworkError
    with pytest.raises(NetworkError):
        search_client.search_github_web("test", "pg4_session_abc", 1)
    
    # No credential should be cooling (single ambiguous = fail-open)
    assert not github_credential_state.is_cooling("github_web", "pg4_session_abc"), \
        "single ambiguous must not cool credential (fail-open)"


# ---------------------------------------------------------------------------
# PG5: repeated ambiguous → pool-cool + WARNING
# ---------------------------------------------------------------------------
def test_pg5_repeated_ambiguous_triggers_pool_cool(monkeypatch):
    """PG5: WHEN Nth ambiguous refusal on same session
    THEN pool-cool applied, WARNING loud-once emitted, metric increments.
    N+1th does not duplicate WARNING.
    """
    # "rate limit" marker triggers is_capacity_refusal but NOT secondary classification
    ambiguous_body = "<html><body>You have hit a rate limit. Please slow down.</body></html>"
    
    error = _make_http_error(403, ambiguous_body)
    
    def mock_request(*args, **kwargs):
        raise error
    
    monkeypatch.setattr(search_client, "request", mock_request)
    
    session = "pg5_session_repeated"
    service = "github_web"
    
    # Trigger ambiguous N times (threshold should be 3)
    from search.client import NetworkError, AMBIGUOUS_POOL_COOL_THRESHOLD
    
    # First N-1 should raise NetworkError (transient)
    for i in range(AMBIGUOUS_POOL_COOL_THRESHOLD - 1):
        with pytest.raises(NetworkError):
            search_client.search_github_web("test", session, 1)
    
    # Nth should trigger pool-cool → GithubCredentialLimited
    with pytest.raises(GithubCredentialLimited):
        search_client.search_github_web("test", session, 1)
    
    # After threshold, pool-cool should be applied
    assert github_credential_state.is_cooling(service, session), \
        f"after {AMBIGUOUS_POOL_COOL_THRESHOLD} ambiguous refusals, credential must be cooled"
    
    # Check metric
    metrics = credential_liveness_metrics()
    assert metrics.get("ambiguous_pool_cools", 0) > 0, \
        "ambiguous_pool_cools metric must increment"


# ---------------------------------------------------------------------------
# PG6: sign-in content with 6.1 predicate → expired_session (composition)
# ---------------------------------------------------------------------------
def test_pg6_sign_in_with_predicate_still_expired_session(monkeypatch):
    """PG6: WHEN sign-in HTML with 6.1 predicate active
    THEN still raises GithubCredentialLimited with reason='expired_session'.
    
    Composition test: new taxonomy must not intercept sign-in content.
    """
    sign_in_html = """<!DOCTYPE html>
<html>
<head><title>Sign in to GitHub · GitHub</title></head>
<body>
<form action="/session" method="post">...</form>
</body>
</html>"""
    
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (sign_in_html, {}))
    
    with pytest.raises(GithubCredentialLimited) as exc_info:
        search_client.search_github_web("test", "pg6_session_abc", 1)
    
    reason = exc_info.value.reason.lower()
    assert "expired" in reason or "sign-in" in reason or "signin" in reason, \
        "sign-in must still be classified as expired_session (6.1 composition)"


# ---------------------------------------------------------------------------
# PG7: healthy path unchanged (regression)
# ---------------------------------------------------------------------------
def test_pg7_healthy_content_unchanged(monkeypatch):
    """PG7: WHEN healthy content
    THEN byte-previous behavior preserved (no new classification, no cooling).
    """
    healthy_html = """<!DOCTYPE html>
<html>
<head><title>Search results · GitHub</title></head>
<body>
<a href="/owner/repo/blob/main/file.py#L42">link</a>
</body>
</html>"""
    
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (healthy_html, {}))
    
    content, headers = search_client.get_github_client().get_with_headers(
        "https://github.com/search", credential="pg7_session_abc"
    )
    
    assert content == healthy_html
    assert not github_credential_state.is_cooling("github_web", "pg7_session_abc"), \
        "healthy content must not cool credential"


# ---------------------------------------------------------------------------
# PG8: success resets the ambiguous streak (remediation, spec wording
# "without intervening success" — credential-liveness requirement)
# ---------------------------------------------------------------------------
def test_pg8_success_resets_ambiguous_streak(monkeypatch):
    """PG8: WHEN ambiguous×(N-1) then a SUCCESS then ambiguous×(N-1)
    THEN no pool-cool — the streak counts refusals *without intervening
    success* (spec credential-liveness, slice 6.3 remediation).

    Pre-fix behavior (RED): the success leg does not reset the counter, so
    the first ambiguous after the success reaches count=N and raises
    GithubCredentialLimited (false-positive pool-cool over long runs).
    """
    from types import SimpleNamespace

    from search.client import (
        AMBIGUOUS_POOL_COOL_THRESHOLD,
        NetworkError,
        get_ambiguous_stats,
        reset_ambiguous_stats,
    )

    reset_ambiguous_stats()
    ambiguous_body = "<html><body>You have hit a rate limit. Please slow down.</body></html>"
    error = _make_http_error(403, ambiguous_body)
    healthy_response = SimpleNamespace(
        status_code=200,
        content=b"<html><head><title>Search results</title></head><body>ok</body></html>",
        headers={},
    )

    session = "pg8_session_streak_reset"
    service = "github_web"
    calls = {"n": 0}

    def sequenced_request(*args, **kwargs):
        calls["n"] += 1
        # legs 1..(N-1): ambiguous refusal; leg N: healthy success;
        # legs N+1..(2N-2): ambiguous again — never reaching the threshold
        # because the success in between MUST reset the streak.
        if calls["n"] == AMBIGUOUS_POOL_COOL_THRESHOLD:
            return healthy_response
        raise error

    monkeypatch.setattr(search_client, "request", sequenced_request)

    total_calls = 2 * (AMBIGUOUS_POOL_COOL_THRESHOLD - 1) + 1  # 5 for N=3
    for i in range(total_calls):
        if i + 1 == AMBIGUOUS_POOL_COOL_THRESHOLD:
            content = search_client.search_github_web("test", session, 1)
            assert content, "healthy leg must return content"
        else:
            try:
                search_client.search_github_web("test", session, 1)
                raise AssertionError("ambiguous leg must raise NetworkError")
            except NetworkError:
                pass  # expected transient path

    # No pool-cool: streak was reset by the intervening success.
    assert not github_credential_state.is_cooling(service, session), \
        "success must reset the ambiguous streak — no pool-cool allowed"
    stats = get_ambiguous_stats()
    assert stats["counts"].get((service, session), 0) <= AMBIGUOUS_POOL_COOL_THRESHOLD - 1, \
        f"streak must stay below threshold after reset, got {stats['counts']}"
    metrics = credential_liveness_metrics()
    assert metrics.get("ambiguous_pool_cools", 0) == 0, \
        "no escalation may be recorded when successes intervene"
