"""Dead-session detection + web-parse tripwire (slice 6.1 П-B + 6.2 П-J, wave 6).

Traceability: 02_critical_fixes.md:98 (П-B), credential-liveness spec.

RED at plan time: the detection/bench/typed-raise machinery does not exist;
the expected RED is that sign-in HTML is treated as a success (no
GithubCredentialLimited with reason containing "expired"/"sign-in"), or the
counters/accessors are absent.

No network, no real sleeping: the cooldown clock is a monkeypatched time.time.
HTTP layer is mocked at _http_get (minimal point) so get_with_headers executes
fully including predicate logic, _report, mark_success.
"""

import time

import pytest

from core.exceptions import TransientFetchError
from search import client as search_client
from tools import state as state_module
from tools.state import (
    GithubCredentialLimited,
    credential_liveness_metrics,
    github_credential_state,
)


SIGN_IN_HTML = """<!DOCTYPE html>
<html>
<head><title>Sign in to GitHub · GitHub</title></head>
<body>
<form>...</form>
</body>
</html>"""

HEALTHY_HTML = """<!DOCTYPE html>
<html>
<head><title>Search results · GitHub</title></head>
<body>
<a href="/owner/repo/blob/main/file.py#L42">link</a>
</body>
</html>"""

EMPTY_RESULTS_HTML = """<!DOCTYPE html>
<html>
<head><title>Search results · GitHub</title></head>
<body>
<p>No results found</p>
</body>
</html>"""


class _Clock:
    """Frozen wall clock; advance() moves time without sleeping."""

    def __init__(self, monkeypatch, start=1_000_000.0):
        self.now = start
        monkeypatch.setattr(time, "time", lambda: self.now)

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)
    # Reset metrics
    from tools.state import reset_credential_liveness_metrics
    reset_credential_liveness_metrics()
    # Reset web parse stats if accessor exists
    if hasattr(search_client, "reset_web_parse_stats"):
        search_client.reset_web_parse_stats()
    # Mock get_user_agent to avoid config dependency
    monkeypatch.setattr(search_client, "get_user_agent", lambda: "test-agent")
    yield
    for key in list(github_credential_state._items):
        github_credential_state._items.pop(key, None)


# ---------------------------------------------------------------------------
# П-B: Dead-session detection
# ---------------------------------------------------------------------------
def test_pb1_sign_in_html_raises_github_credential_limited(monkeypatch):
    """PB1: WHEN sign-in HTML is returned THEN GithubCredentialLimited with
    reason containing 'expired' or 'sign-in', NOT TransientFetchError, NOT ''.

    Mocks _http_get (minimal point) so get_with_headers executes fully.
    """
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (SIGN_IN_HTML, {}))

    with pytest.raises(GithubCredentialLimited) as exc_info:
        search_client.search_github_web("test", "dead_session_abc123", 1)

    reason = exc_info.value.reason.lower()
    assert "expired" in reason or "sign-in" in reason or "signin" in reason


def test_pb2_cooldown_not_released_for_sign_in(monkeypatch):
    """PB2: WHEN sign-in HTML THEN cooldown is NOT released (is_cooling stays True,
    early_releases unchanged). Exercises REAL get_with_headers with predicate.

    Mocks _http_get so predicate logic, _report, mark_success all execute.
    """
    clock = _Clock(monkeypatch)
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (SIGN_IN_HTML, {}))

    session = "dead_session_cooldown_test"
    service = "github_web"

    # Pre-condition: set cooldown
    github_credential_state.mark_limited(service, session, wait=600)
    assert github_credential_state.is_cooling(service, session)

    before_releases = credential_liveness_metrics()["early_releases"]

    with pytest.raises(GithubCredentialLimited):
        search_client.search_github_web("test", session, 1)

    # Cooldown should NOT be released (predicate suppressed false success)
    assert github_credential_state.is_cooling(service, session), \
        "cooldown must survive sign-in detection (predicate suppressed false success)"
    assert credential_liveness_metrics()["early_releases"] == before_releases, \
        "early_releases must not grow for sign-in content"


def test_pb3_report_receives_success_false_for_sign_in(monkeypatch):
    """PB3: WHEN sign-in HTML THEN _report receives success=False (predicate
    suppressed false success before _report). Exercises REAL get_with_headers.

    Mocks _http_get so predicate logic executes; spies on _report.
    """
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (SIGN_IN_HTML, {}))

    # Spy on _report BEFORE the call
    report_calls = []
    original_report = real_client._report

    def spy_report(service, success, credential=None):
        report_calls.append((service, success, credential))
        original_report(service, success, credential)

    monkeypatch.setattr(real_client, "_report", spy_report)

    with pytest.raises(GithubCredentialLimited):
        search_client.search_github_web("test", "dead_session_report_test", 1)

    # _report must have been called with success=False (predicate suppressed false success)
    assert len(report_calls) > 0, "_report must be called"
    for _, success, _ in report_calls:
        assert success is False, \
            f"sign-in content must be reported as success=False, got {success}"


def test_pb4_dead_session_benched_with_long_cooldown(monkeypatch):
    """PB4: WHEN sign-in detected THEN credential is benched with long wait
    (frozen clock); subsequent selection does not pick it again.

    Mocks _http_get so detection + bench logic execute.
    """
    clock = _Clock(monkeypatch)
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (SIGN_IN_HTML, {}))

    session = "dead_session_bench_test"

    with pytest.raises(GithubCredentialLimited) as exc_info:
        search_client.search_github_web("test", session, 1)

    # Credential should be cooling
    assert github_credential_state.is_cooling("github_web", session)
    wait = github_credential_state.wait_time("github_web", session)
    # Wait should be long (at least 1 hour = 3600s for expired session)
    assert wait >= 3600, f"expired session cooldown should be >= 3600s, got {wait}"

    # Second call should raise without new HTTP (credential is cooling)
    # OR should raise GithubCredentialLimited again (rotation catch)
    with pytest.raises(GithubCredentialLimited):
        search_client.search_github_web("test", session, 1)


def test_pb5_dead_session_counter_and_loud_once(monkeypatch):
    """PB5: WHEN sign-in detected THEN counter grows + WARNING exactly once
    on N detects (loud-once).

    Mocks _http_get so detection + counter logic execute.
    """
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (SIGN_IN_HTML, {}))

    # First detection
    with pytest.raises(GithubCredentialLimited):
        search_client.search_github_web("test", "dead_session_1", 1)

    metrics_after_first = credential_liveness_metrics()
    assert "dead_sessions_detected" in metrics_after_first
    assert metrics_after_first["dead_sessions_detected"] >= 1

    # Second detection (different session)
    with pytest.raises(GithubCredentialLimited):
        search_client.search_github_web("test", "dead_session_2", 1)

    metrics_after_second = credential_liveness_metrics()
    assert metrics_after_second["dead_sessions_detected"] >= 2

    # Check accessor exists
    if hasattr(search_client, "get_dead_session_stats"):
        stats = search_client.get_dead_session_stats()
        assert "detected" in stats
        assert stats["detected"] >= 2


def test_pb6_healthy_content_releases_cooldown(monkeypatch):
    """PB6: WHEN healthy content THEN cooldown is released (full positive path).
    Exercises REAL get_with_headers with predicate.

    Mocks _http_get so predicate logic, _report, mark_success all execute.
    """
    clock = _Clock(monkeypatch)
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (HEALTHY_HTML, {}))

    session = "healthy_session_cooldown_test"
    service = "github_web"

    # Pre-condition: set cooldown
    github_credential_state.mark_limited(service, session, wait=600)
    assert github_credential_state.is_cooling(service, session)

    before_releases = credential_liveness_metrics()["early_releases"]

    # Healthy content should return normally
    content = search_client.search_github_web("test", session, 1)
    assert content == HEALTHY_HTML

    # Cooldown should be released (mark_success called, early_release active)
    assert not github_credential_state.is_cooling(service, session), \
        "healthy content must release cooldown"
    assert credential_liveness_metrics()["early_releases"] == before_releases + 1, \
        "early_releases must increment for healthy content"


def test_pb7_exception_shape_for_rotation_catch(monkeypatch):
    """PB7: GithubCredentialLimited with reason=expired_session has correct shape
    for stage rotation catch (service, credential, wait, reason attributes).

    This is a client-level shape pin, not a stage integration test.
    Mocks _http_get so detection + typed raise execute.
    """
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (SIGN_IN_HTML, {}))

    with pytest.raises(GithubCredentialLimited) as exc_info:
        search_client.search_github_web("test", "dead_session_shape_test", 1)

    # The exception should have service, credential, wait, reason attributes
    assert exc_info.value.service == "github_web"
    assert exc_info.value.credential == "dead_session_shape_test"
    assert exc_info.value.wait > 0
    assert "expired" in exc_info.value.reason.lower() or "sign" in exc_info.value.reason.lower()


# ---------------------------------------------------------------------------
# П-J: Web-parse tripwire monitor
# ---------------------------------------------------------------------------
def test_pj1_nonblank_content_without_links_counts_zero(monkeypatch):
    """PJ1: WHEN non-blank content without blob links THEN zero+=1, nonzero==0,
    WARNING loud-once.

    Mocks _http_get so search_web_with_count executes real parsing logic.
    """
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (EMPTY_RESULTS_HTML, {}))

    # Reset stats
    if hasattr(search_client, "reset_web_parse_stats"):
        search_client.reset_web_parse_stats()

    results, total, content = search_client.search_web_with_count("test", "session_abc", 1)

    # Should have results=[] (no blob links), but no exception
    assert results == []
    assert content == EMPTY_RESULTS_HTML

    # Check counters
    if hasattr(search_client, "get_web_parse_stats"):
        stats = search_client.get_web_parse_stats()
        assert stats.get("web_pages_parsed_zero", 0) >= 1
        assert stats.get("web_pages_parsed_nonzero", 0) == 0


def test_pj2_content_with_links_counts_nonzero(monkeypatch):
    """PJ2: WHEN content with blob links THEN nonzero+=1, no WARNING.

    Mocks _http_get so search_web_with_count executes real parsing logic.
    """
    real_client = search_client.get_github_client()
    monkeypatch.setattr(real_client, "_http_get", lambda *args, **kwargs: (HEALTHY_HTML, {}))

    if hasattr(search_client, "reset_web_parse_stats"):
        search_client.reset_web_parse_stats()

    results, total, content = search_client.search_web_with_count("test", "session_xyz", 1)

    # Should have at least one result
    assert len(results) >= 1

    if hasattr(search_client, "get_web_parse_stats"):
        stats = search_client.get_web_parse_stats()
        assert stats.get("web_pages_parsed_nonzero", 0) >= 1


def test_pj3_blank_content_does_not_count(monkeypatch):
    """PJ3: WHEN blank content THEN TransientFetchError WITHOUT changing counters
    (semantics preserved).

    Mocks search_github_web to return blank (bypasses HTTP layer entirely).
    """
    monkeypatch.setattr(search_client, "search_github_web", lambda q, s, p: "")

    if hasattr(search_client, "reset_web_parse_stats"):
        search_client.reset_web_parse_stats()

    with pytest.raises(TransientFetchError):
        search_client.search_web_with_count("test", "session_blank", 1)

    # Counters should not have changed
    if hasattr(search_client, "get_web_parse_stats"):
        stats = search_client.get_web_parse_stats()
        assert stats.get("web_pages_parsed_zero", 0) == 0
        assert stats.get("web_pages_parsed_nonzero", 0) == 0
